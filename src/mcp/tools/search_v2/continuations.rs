//! Stateless, resolved search continuations.
use std::path::{Component, Path};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::index::bloom::BloomFilterCache;
use crate::search::{callees, callers, grok};
use crate::types::is_test_file;

const SECTION_CAP: usize = 30;
/// Deps enrichment is best-effort: a cold or stale index must never block a
/// search, so reconcile/impact get this much wall clock and then report partial.
const DEPS_WARM_DEADLINE: Duration = Duration::from_millis(200);

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Target {
    pub(super) path: String,
    pub(super) line: Option<u32>,
    pub(super) name: Option<String>,
    pub(super) scope: String,
    pub(super) glob: Option<String>,
}

impl Target {
    pub(super) fn hints(&self) -> Vec<Value> {
        let kinds: &[&str] = if self.line.is_some() {
            &[
                "fetch_callers",
                "fetch_callees",
                "fetch_siblings",
                "fetch_tests",
                "fetch_dependencies",
            ]
        } else {
            &["fetch_dependencies"]
        };
        kinds
            .iter()
            .map(|kind| json!({"kind": kind, "target": self}))
            .collect()
    }

    pub(super) fn allows(&self, path: &Path, cwd: &Path) -> bool {
        let canonical_cwd = cwd.canonicalize().unwrap_or_else(|_| cwd.to_path_buf());
        let canonical_path = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
        let cwd = canonical_cwd.as_path();
        let path = canonical_path.as_path();
        if !path.starts_with(cwd) {
            return false;
        }
        let Some(pattern) = self.glob.as_deref().filter(|s| !s.is_empty()) else {
            return true;
        };
        let mut builder = ignore::overrides::OverrideBuilder::new(cwd);
        builder.add(pattern).is_ok()
            && builder
                .build()
                .is_ok_and(|filter| !filter.matched(path, false).is_ignore())
    }

    fn validate(&self, cwd: &Path) -> Result<(), String> {
        if self.scope != cwd.to_string_lossy() {
            return Err("follow target scope does not match cwd".into());
        }
        if Path::new(&self.path).is_absolute() {
            return Err("follow target requires a cwd-relative path".into());
        }
        if self.path.is_empty()
            || Path::new(&self.path)
                .components()
                .any(|c| c == Component::ParentDir)
        {
            return Err("follow target requires a normalized path".into());
        }
        let full = cwd.join(&self.path);
        if !full.is_file() {
            return Err("follow target must be an existing file".into());
        }
        // Containment is unconditional: canonicalize so a symlink inside cwd
        // cannot point the concrete target outside the declared scope.
        let canonical_cwd = cwd.canonicalize().map_err(|e| e.to_string())?;
        if !full
            .canonicalize()
            .map_err(|e| e.to_string())?
            .starts_with(&canonical_cwd)
        {
            return Err("follow target is outside cwd".into());
        }
        if self.line == Some(0)
            || self.line.is_some() != self.name.is_some()
            || self.name.as_ref().is_some_and(String::is_empty)
        {
            return Err("follow symbol target requires a positive line and nonempty name".into());
        }
        crate::search::walker(cwd, self.glob.as_deref()).map_err(|e| e.to_string())?;
        if self.glob.is_some() && !self.allows(&full, cwd) {
            return Err("follow target is outside its glob".into());
        }
        if let Some(line) = self.line {
            let spec = format!("{}:{line}", full.display());
            let (target, _, _) =
                grok::resolve_with_source(&spec, cwd).map_err(|e| e.to_string())?;
            if target.start_line != line || Some(&target.name) != self.name.as_ref() {
                return Err("follow target identity changed; search again".into());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Follow {
    pub kind: String,
    target: Target,
}

impl Follow {
    pub fn parse(value: &Value, cwd: &Path) -> Result<Self, String> {
        let kind = value
            .get("kind")
            .and_then(Value::as_str)
            .ok_or("follow hint requires kind")?;
        if !matches!(
            kind,
            "fetch_callers"
                | "fetch_callees"
                | "fetch_siblings"
                | "fetch_tests"
                | "fetch_dependencies"
        ) {
            return Err("unknown continuation kind".into());
        }
        if value
            .get("target")
            .is_some_and(|t| t.get("line").is_none() || t.get("name").is_none())
        {
            return Err("follow target requires line and name; use null for file targets".into());
        }
        let follow: Self = serde_json::from_value(value.clone())
            .map_err(|e| format!("invalid follow hint: {e}"))?;
        follow.target.validate(cwd)?;
        if follow.kind != "fetch_dependencies" && follow.target.line.is_none() {
            return Err("this continuation requires a symbol target".into());
        }
        Ok(follow)
    }

    pub fn execute(
        &self,
        cwd: &Path,
        bloom: &BloomFilterCache,
        client: &str,
    ) -> Result<Value, String> {
        let query = self.target.name.as_deref().unwrap_or(&self.target.path);
        let mut result = super::base_result(query, &self.kind, "ok");
        result["target"] = json!(self.target);
        if self.kind == "fetch_dependencies" {
            let deps = dependencies(&self.target, cwd, client)?;
            let empty = deps["imports"].as_array().unwrap().is_empty()
                && deps["dependents"].as_array().unwrap().is_empty();
            if deps["coverage"] != "complete" {
                super::mark_partial(&mut result);
            } else if empty {
                result["status"] = json!("no_match");
            }
            result["preview"] = json!(deps.to_string());
            result["dependency_impact"] = deps;
            return Ok(result);
        }
        let full = cwd.join(&self.target.path);
        let spec = format!("{}:{}", full.display(), self.target.line.unwrap());
        let (target, content, lang) =
            grok::resolve_with_source(&spec, cwd).map_err(|e| e.to_string())?;
        let mut partial = false;
        let mut items = Vec::new();
        match self.kind.as_str() {
            "fetch_siblings" => {
                let entries = crate::lang::outline::get_outline_entries(&content, lang);
                items = grok::collect_siblings(&entries, &target)
                    .into_iter()
                    .map(|s| {
                        let signature =
                            s.signature.map(|sig| super::redact_secret_text(&full, sig));
                        json!({"path": self.target.path, "name": s.name, "line": s.start_line,
                        "end_line": s.end_line, "signature": signature})
                    })
                    .collect();
            }
            "fetch_callees" => {
                let names = callees::extract_callee_names(
                    &content,
                    lang,
                    Some((target.start_line, target.end_line)),
                );
                let resolved = callees::resolve_callees(&names, &full, &content, bloom);
                // Unresolved names are not verified external calls.
                partial = names.iter().any(|n| !resolved.iter().any(|c| &c.name == n));
                let candidates: Vec<_> = resolved
                    .into_iter()
                    .filter(|c| !(c.file == full && c.start_line == target.start_line))
                    .collect();
                let candidate_count = candidates.len();
                let in_scope: Vec<_> = candidates
                    .into_iter()
                    .filter(|c| self.target.allows(&c.file, cwd))
                    .collect();
                // Scope-dropped callees are real callees the caller cannot see.
                partial |= in_scope.len() < candidate_count;
                items = in_scope
                    .into_iter()
                    .map(|c| {
                        let signature = c
                            .signature
                            .map(|sig| super::redact_secret_text(&c.file, sig));
                        json!({"path": super::display_rel(&c.file, cwd), "name": c.name,
                        "line": c.start_line, "end_line": c.end_line, "signature": signature})
                    })
                    .collect();
            }
            "fetch_callers" | "fetch_tests" => {
                let names = std::iter::once(target.name.clone()).collect();
                let (matches, unreadable) = callers::find_callers_batch(
                    &names,
                    cwd,
                    bloom,
                    self.target.glob.as_deref(),
                    callers::BATCH_EARLY_QUIT,
                )
                .map_err(|e| e.to_string())?;
                partial = unreadable > 0 || matches.len() >= callers::BATCH_EARLY_QUIT;
                for (_, caller) in matches {
                    if is_test_file(&caller.path) != (self.kind == "fetch_tests") {
                        continue;
                    }
                    if !self.target.allows(&caller.path, cwd) {
                        continue;
                    }
                    // Bind same-name candidates to the resolved definition through the existing import resolver.
                    let resolved = callees::resolve_callees(
                        std::slice::from_ref(&target.name),
                        &caller.path,
                        &caller.content,
                        bloom,
                    );
                    if resolved.is_empty() {
                        partial = true;
                        continue;
                    }
                    if !resolved
                        .iter()
                        .any(|c| c.file == full && c.start_line == target.start_line)
                    {
                        continue;
                    }
                    if caller.path == full
                        && (target.start_line..=target.end_line).contains(&caller.line)
                    {
                        continue;
                    }
                    let call = super::redact_secret_text(&caller.path, caller.call_text);
                    items.push(
                        json!({"path": super::display_rel(&caller.path, cwd), "line": caller.line,
                        "name": caller.calling_function, "call": call}),
                    );
                }
            }
            _ => unreachable!(),
        }
        items.sort_by_key(|item| {
            (
                item["path"].as_str().unwrap_or("").to_string(),
                item["line"].as_u64().unwrap_or(0),
            )
        });
        let total = items.len();
        partial |= total > SECTION_CAP;
        items.truncate(SECTION_CAP);
        if partial {
            super::mark_partial(&mut result);
        } else if items.is_empty() {
            result["status"] = json!("no_match");
        }
        result["total_found"] = json!(total);
        result["preview"] = json!(items
            .iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join("\n"));
        result["items"] = json!(items);
        Ok(result)
    }
}

pub(super) fn dependencies(target: &Target, cwd: &Path, client: &str) -> Result<Value, String> {
    dependencies_until(target, cwd, client, Instant::now() + DEPS_WARM_DEADLINE)
}

fn dependencies_until(
    target: &Target,
    cwd: &Path,
    client: &str,
    deadline: Instant,
) -> Result<Value, String> {
    let full = cwd
        .join(&target.path)
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let content = std::fs::read_to_string(&full).map_err(|e| e.to_string())?;
    let mut imports: Vec<_> =
        crate::read::imports::resolve_related_files_with_content(&full, &content)
            .into_iter()
            .filter(|p| target.allows(p, cwd))
            .map(|p| super::display_rel(&p, cwd))
            .collect();
    imports.sort();
    imports.dedup();
    let (refresh, impact, state) = match crate::index::deps::open(cwd, client) {
        Ok(handle) => {
            let refresh = crate::index::deps::reconcile(&handle, handle.worktree_root(), deadline);
            let impact = crate::index::deps::impact(&handle, &full, deadline);
            (refresh, Some(impact), "open")
        }
        Err(_) => (crate::index::deps::Coverage::default(), None, "unavailable"),
    };
    let traversal = impact.as_ref().map(|i| i.coverage).unwrap_or_default();
    let mut dependents: Vec<_> = impact
        .into_iter()
        .flat_map(|i| i.dependents)
        .filter(|p| target.allows(p, cwd))
        .map(|p| super::display_rel(&p, cwd))
        .collect();
    dependents.sort();
    dependents.dedup();
    let total_imports = imports.len();
    let total_dependents = dependents.len();
    let complete = refresh.complete
        && traversal.complete
        && total_imports <= SECTION_CAP
        && total_dependents <= SECTION_CAP;
    imports.truncate(SECTION_CAP);
    dependents.truncate(SECTION_CAP);
    Ok(
        json!({"coverage": if complete { "complete" } else { "partial" },
        "index_state": state, "timed_out": refresh.timed_out || traversal.timed_out,
        "refresh": {"complete": refresh.complete, "files_scanned": refresh.files_scanned, "files_changed": refresh.files_changed},
        "traversal": {"complete": traversal.complete, "files_scanned": traversal.files_scanned},
        "imports": imports, "dependents": dependents,
        "total_imports": total_imports, "total_dependents": total_dependents}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn follow_hint(cwd: &Path, kind: &str, glob: &Value) -> Value {
        json!({"kind": kind, "target": {"path": "root.ts", "line": 2, "name": "root",
            "scope": cwd.to_string_lossy(), "glob": glob}})
    }

    fn scoped_fixture() -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("helper.ts"), "export function leaf() {}\n").unwrap();
        std::fs::write(
            tmp.path().join("root.ts"),
            "import { leaf } from './helper';\nexport function root() { leaf(); }\n",
        )
        .unwrap();
        std::fs::write(
            tmp.path().join("caller.ts"),
            "import { root } from './root';\nfunction outside_caller() { root(); }\n",
        )
        .unwrap();
        tmp
    }

    fn run(cwd: &Path, hint: &Value) -> Value {
        Follow::parse(hint, cwd)
            .unwrap()
            .execute(cwd, &BloomFilterCache::new(), "scope-test")
            .unwrap()
    }

    #[test]
    fn glob_scoped_follows_exclude_out_of_scope_items() {
        let tmp = scoped_fixture();
        let cwd = tmp.path();

        let unscoped_callees = run(cwd, &follow_hint(cwd, "fetch_callees", &Value::Null));
        assert_eq!(unscoped_callees["items"][0]["name"], "leaf");
        let scoped_callees = run(cwd, &follow_hint(cwd, "fetch_callees", &json!("root.ts")));
        assert!(scoped_callees["items"].as_array().unwrap().is_empty());
        assert_eq!(scoped_callees["completeness"], "partial");

        let unscoped_callers = run(cwd, &follow_hint(cwd, "fetch_callers", &Value::Null));
        assert_eq!(unscoped_callers["items"][0]["name"], "outside_caller");
        let scoped_callers = run(cwd, &follow_hint(cwd, "fetch_callers", &json!("root.ts")));
        assert!(scoped_callers["items"].as_array().unwrap().is_empty());
    }

    #[test]
    fn targets_outside_cwd_are_rejected() {
        let tmp = scoped_fixture();
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(
            outside.path().join("secret.ts"),
            "export function root() {}\n",
        )
        .unwrap();

        let mut absolute = follow_hint(tmp.path(), "fetch_callers", &Value::Null);
        absolute["target"]["path"] = json!(outside.path().join("secret.ts").to_string_lossy());
        let err = Follow::parse(&absolute, tmp.path()).unwrap_err();
        assert!(err.contains("cwd-relative"), "{err}");

        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(
                outside.path().join("secret.ts"),
                tmp.path().join("linked.ts"),
            )
            .unwrap();
            let mut linked = follow_hint(tmp.path(), "fetch_callers", &Value::Null);
            linked["target"]["path"] = json!("linked.ts");
            linked["target"]["line"] = json!(1);
            let err = Follow::parse(&linked, tmp.path()).unwrap_err();
            assert!(err.contains("outside cwd"), "{err}");
        }
    }

    #[test]
    fn expired_dependency_deadline_reports_actual_partial_coverage() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(std::process::Command::new("git")
            .args(["init", "-q"])
            .current_dir(tmp.path())
            .status()
            .unwrap()
            .success());
        std::fs::write(tmp.path().join("root.rs"), "fn root() {}\n").unwrap();
        let target = Target {
            path: "root.rs".into(),
            line: Some(1),
            name: Some("root".into()),
            scope: tmp.path().to_string_lossy().into(),
            glob: None,
        };
        let result =
            dependencies_until(&target, tmp.path(), "deadline-test", Instant::now()).unwrap();
        assert_eq!(result["coverage"], "partial");
        assert_eq!(result["timed_out"], true);
        assert_eq!(result["refresh"]["complete"], false);
        assert_eq!(result["refresh"]["files_scanned"], 0);
    }
}
