//! Stateless, resolved search continuations.
use std::path::{Component, Path};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::cache::OutlineCache;
use crate::index::bloom::BloomFilterCache;
use crate::search::{callees, callers, target};
use crate::types::is_test_file;

const SECTION_CAP: usize = 30;

fn normalized(path: &Path) -> std::path::PathBuf {
    path.components().collect()
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Target {
    pub(super) path: String,
    pub(super) line: Option<u32>,
    pub(super) name: Option<String>,
    /// Optional source-byte range that distinguishes same-line declarations.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) occurrence: Option<(usize, usize)>,
    /// The searched directory: `.` for cwd, a cwd-relative path inside it, or
    /// an absolute path (trusted as-is) outside it.
    pub(super) scope: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) glob: Option<String>,
}

impl Target {
    pub(super) fn hints(&self) -> Vec<Value> {
        if self.line.is_none() {
            return Vec::new();
        }
        [
            "fetch_callers",
            "fetch_callees",
            "fetch_siblings",
            "fetch_tests",
        ]
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

    /// Anchor a relative scope under `cwd`; refuse `..`; trust an absolute scope.
    fn resolve_scope(&self, cwd: &Path) -> Result<std::path::PathBuf, String> {
        let scope = Path::new(&self.scope);
        if scope.is_absolute() {
            return Ok(normalized(scope));
        }
        if scope.components().any(|c| c == Component::ParentDir) {
            return Err("follow target scope requires a normalized path".into());
        }
        Ok(normalized(&cwd.join(scope)))
    }

    fn validate(&self, cwd: &Path, cache: &OutlineCache) -> Result<(), String> {
        if self.resolve_scope(cwd)? != normalized(cwd) {
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
            let (target, _, _) = match (self.name.as_deref(), self.occurrence) {
                (Some(name), Some(occurrence)) => target::resolve_by_path_line_occurrence(
                    &full,
                    line,
                    name,
                    occurrence,
                    (cwd, self.glob.as_deref()),
                    cache,
                ),
                _ => target::resolve_by_path_line(&full, line, cache),
            }
            .map_err(|e| e.to_string())?;
            if target.start_line != line || Some(&target.name) != self.name.as_ref() {
                return Err("follow target identity changed; search again".into());
            }
            self.validate_occurrence(&full, cwd, cache)?;
        }
        Ok(())
    }

    fn validate_occurrence(
        &self,
        full: &Path,
        cwd: &Path,
        cache: &OutlineCache,
    ) -> Result<(), String> {
        let Some(occurrence) = self.occurrence else {
            return Ok(());
        };
        let Some(name) = self.name.as_deref() else {
            return Err("follow occurrence requires a symbol target".into());
        };
        let result =
            crate::search::search_symbol_raw_cached(name, cwd, self.glob.as_deref(), cache)
                .map_err(|e| e.to_string())?;
        let canonical = full.canonicalize().map_err(|e| e.to_string())?;
        let matches = result.matches.iter().filter(|candidate| {
            candidate.is_definition
                && candidate.path.canonicalize().ok().as_ref() == Some(&canonical)
                && candidate.line == self.line.unwrap_or_default()
                && candidate.def_name.as_deref() == Some(name)
                && candidate.def_byte_range == Some(occurrence)
        });
        if matches.count() != 1 {
            return Err("follow target occurrence changed; search again".into());
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
    pub fn parse(value: &Value, cwd: &Path, cache: &OutlineCache) -> Result<Self, String> {
        let kind = value
            .get("kind")
            .and_then(Value::as_str)
            .ok_or("follow hint requires kind")?;
        if !matches!(
            kind,
            "fetch_callers" | "fetch_callees" | "fetch_siblings" | "fetch_tests"
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
        follow.target.validate(cwd, cache)?;
        if follow.target.line.is_none() {
            return Err("this continuation requires a symbol target".into());
        }
        Ok(follow)
    }

    pub fn execute(
        &self,
        cwd: &Path,
        bloom: &BloomFilterCache,
        cache: &OutlineCache,
    ) -> Result<Value, String> {
        let query = self.target.name.as_deref().unwrap_or(&self.target.path);
        let mut result = super::base_result(query, &self.kind, "ok");
        result["target"] = json!(self.target);
        let full = cwd.join(&self.target.path);
        let line = self.target.line.unwrap();
        let (target, content, lang) = match (self.target.name.as_deref(), self.target.occurrence) {
            (Some(name), Some(occurrence)) => target::resolve_by_path_line_occurrence(
                &full,
                line,
                name,
                occurrence,
                (cwd, self.target.glob.as_deref()),
                cache,
            ),
            _ => target::resolve_by_path_line(&full, line, cache),
        }
        .map_err(|e| e.to_string())?;
        self.target.validate_occurrence(&full, cwd, cache)?;
        let parsed = cache.parse_source(&full, &content);
        let mut partial = false;
        let mut items = Vec::new();
        let target_span_start = target.span_start_line;
        match self.kind.as_str() {
            "fetch_siblings" => {
                let entries = parsed.as_ref().map_or_else(
                    || crate::lang::outline::get_deep_outline_tree(&content, lang),
                    |parsed| {
                        crate::lang::outline::deep_outline_tree_from_tree(
                            parsed.content(),
                            lang,
                            parsed.tree(),
                        )
                    },
                );
                items = target::collect_siblings(&entries, &target)
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
                let range = Some((target_span_start, target.end_line));
                let names = parsed.as_ref().map_or_else(
                    || callees::extract_callee_names(&content, lang, range),
                    |parsed| {
                        callees::extract_callee_names_from_tree(
                            parsed.content(),
                            lang,
                            parsed.tree(),
                            range,
                        )
                    },
                );
                let resolved =
                    callees::resolve_callees_cached(&names, &full, &content, bloom, cache);
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
                let (matches, unreadable) = callers::find_callers_batch_cached(
                    &names,
                    cwd,
                    bloom,
                    self.target.glob.as_deref(),
                    callers::BATCH_EARLY_QUIT,
                    cache,
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
                    let resolved = callees::resolve_callees_cached(
                        std::slice::from_ref(&target.name),
                        &caller.path,
                        caller.snapshot.content(),
                        bloom,
                        cache,
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
                        && (target_span_start..=target.end_line).contains(&caller.line)
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
        result["items"] = json!(items);
        Ok(result)
    }
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
        let cache = OutlineCache::new();
        Follow::parse(hint, cwd, &cache)
            .unwrap()
            .execute(cwd, &BloomFilterCache::new(), &cache)
            .unwrap()
    }

    #[test]
    fn emitted_hints_validate_against_follow_schema() {
        let tools = crate::mcp::tools::definitions::tool_definitions();
        let search = tools.iter().find(|t| t["name"] == "tilth_search").unwrap();
        let follow = &search["inputSchema"]["properties"]["queries"]["items"]["oneOf"][2]
            ["properties"]["follow"];
        let schema_target = &follow["properties"]["target"];
        let props = schema_target["properties"].as_object().unwrap();
        let required: Vec<&str> = schema_target["required"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(Value::as_str)
            .collect();
        let kinds = follow["properties"]["kind"]["enum"].as_array().unwrap();
        let target = Target {
            path: "a.rs".into(),
            line: Some(3),
            name: Some("f".into()),
            occurrence: Some((10, 20)),
            scope: ".".into(),
            glob: Some("*.rs".into()),
        };
        let hints = target.hints();
        assert_eq!(hints.len(), 4);
        for hint in hints {
            assert!(kinds.contains(&hint["kind"]), "kind not in schema: {hint}");
            let emitted = hint["target"].as_object().unwrap();
            for key in emitted.keys() {
                assert!(props.contains_key(key), "schema lacks emitted key {key}");
            }
            for key in &required {
                assert!(emitted.contains_key(*key), "hint lacks required key {key}");
            }
            let occ = props["occurrence"]["items"]["minimum"].as_u64().unwrap();
            assert!(emitted["occurrence"]
                .as_array()
                .unwrap()
                .iter()
                .all(|n| n.as_u64().unwrap() >= occ));
        }
    }

    #[test]
    fn glob_scoped_follows_exclude_out_of_scope_items() {
        let tmp = scoped_fixture();
        let cwd = tmp.path();

        let unscoped_callees = run(cwd, &follow_hint(cwd, "fetch_callees", &Value::Null));
        assert_eq!(unscoped_callees["items"][0]["name"], "leaf");
        let scoped_callees = run(cwd, &follow_hint(cwd, "fetch_callees", &json!("root.ts")));
        assert_eq!(
            scoped_callees["items"].as_array().unwrap().as_slice(),
            [] as [serde_json::Value; 0]
        );
        assert_eq!(scoped_callees["completeness"], "partial");

        let unscoped_callers = run(cwd, &follow_hint(cwd, "fetch_callers", &Value::Null));
        assert_eq!(unscoped_callers["items"][0]["name"], "outside_caller");
        let scoped_callers = run(cwd, &follow_hint(cwd, "fetch_callers", &json!("root.ts")));
        assert_eq!(
            scoped_callers["items"].as_array().unwrap().as_slice(),
            [] as [serde_json::Value; 0]
        );
    }

    #[test]
    fn relative_scope_anchors_under_cwd() {
        let tmp = scoped_fixture();
        let cwd = tmp.path();
        let cache = OutlineCache::new();
        for scope in [".", "./"] {
            let mut hint = follow_hint(cwd, "fetch_callees", &Value::Null);
            hint["target"]["scope"] = json!(scope);
            hint["target"].as_object_mut().unwrap().remove("glob");
            assert_eq!(run(cwd, &hint)["items"][0]["name"], "leaf", "{scope}");
        }
        for scope in ["..", "sub/..", "sub", "/tmp"] {
            let mut hint = follow_hint(cwd, "fetch_callees", &Value::Null);
            hint["target"]["scope"] = json!(scope);
            assert!(Follow::parse(&hint, cwd, &cache).is_err(), "{scope}");
        }
    }

    #[test]
    fn hints_omit_null_optional_keys() {
        let target = Target {
            path: "root.ts".into(),
            line: Some(2),
            name: Some("root".into()),
            occurrence: None,
            scope: ".".into(),
            glob: None,
        };
        let hint = &target.hints()[0];
        assert_eq!(
            hint["target"],
            json!({"path": "root.ts", "line": 2, "name": "root", "scope": "."})
        );
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
        let err = Follow::parse(&absolute, tmp.path(), &OutlineCache::new()).unwrap_err();
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
            let err = Follow::parse(&linked, tmp.path(), &OutlineCache::new()).unwrap_err();
            assert!(err.contains("outside cwd"), "{err}");
        }
    }

    #[test]
    fn stale_same_line_occurrence_is_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("dupes.rs"), "fn run() {} fn run() {}\n").unwrap();
        let found = crate::search::search_symbol_raw("run", tmp.path(), None).unwrap();
        let ranges: Vec<_> = found
            .matches
            .iter()
            .filter(|candidate| candidate.is_definition)
            .filter_map(|candidate| candidate.def_byte_range)
            .collect();
        assert_eq!(ranges.len(), 2);

        let hint = |occurrence| {
            json!({"kind": "fetch_siblings", "target": {
                "path": "dupes.rs", "line": 1, "name": "run", "occurrence": occurrence,
                "scope": tmp.path().to_string_lossy(), "glob": null
            }})
        };
        Follow::parse(&hint(ranges[1]), tmp.path(), &OutlineCache::new())
            .expect("current occurrence is valid");
        std::fs::write(tmp.path().join("dupes.rs"), "fn run() {}\n").unwrap();
        let err = Follow::parse(&hint(ranges[1]), tmp.path(), &OutlineCache::new()).unwrap_err();
        assert!(err.contains("occurrence changed"), "{err}");
    }

    #[test]
    fn glob_scoped_follows_resolve_among_many_same_name_defs() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir(tmp.path().join("render")).unwrap();
        for n in 0..30 {
            std::fs::write(
                tmp.path().join(format!("render/r{n:02}.go")),
                "package render\n\nfunc Render() {}\n",
            )
            .unwrap();
        }
        std::fs::write(
            tmp.path().join("context.go"),
            "package gin\n\nfunc Render() { helper() }\n\nfunc helper() {}\n",
        )
        .unwrap();
        let found = crate::search::search_symbol_raw_cached(
            "Render",
            tmp.path(),
            Some("context.go"),
            &OutlineCache::new(),
        )
        .unwrap();
        let occurrence = found
            .matches
            .iter()
            .find(|candidate| candidate.is_definition)
            .and_then(|candidate| candidate.def_byte_range)
            .expect("context.go occurrence");
        for kind in [
            "fetch_callers",
            "fetch_callees",
            "fetch_siblings",
            "fetch_tests",
        ] {
            let hint = json!({"kind": kind, "target": {
                "path": "context.go", "line": 3, "name": "Render", "occurrence": occurrence,
                "scope": tmp.path().to_string_lossy(), "glob": "context.go"
            }});
            let result = run(tmp.path(), &hint);
            assert!(
                matches!(result["status"].as_str(), Some("ok" | "no_match")),
                "{kind}: {result}"
            );
        }
    }

    #[test]
    fn deep_target_returns_real_siblings() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(
            tmp.path().join("nested.rs"),
            "mod outer {\n    mod inner {\n        fn target() {}\n        fn sibling() {}\n    }\n}\n",
        )
        .unwrap();
        let found = crate::search::search_symbol_raw("target", tmp.path(), None).unwrap();
        let occurrence = found
            .matches
            .iter()
            .find(|candidate| candidate.is_definition)
            .and_then(|candidate| candidate.def_byte_range)
            .expect("target occurrence");
        let hint = json!({"kind": "fetch_siblings", "target": {
            "path": "nested.rs", "line": 3, "name": "target", "occurrence": occurrence,
            "scope": tmp.path().to_string_lossy(), "glob": null
        }});
        let result = run(tmp.path(), &hint);
        assert_eq!(result["status"], "ok", "{result}");
        assert!(
            result["items"]
                .as_array()
                .unwrap()
                .iter()
                .any(|item| { item["name"] == "sibling" && item["line"] == 4 }),
            "{result}"
        );
    }
}
