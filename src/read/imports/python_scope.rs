//! Scoped resolution of *absolute* Python imports to in-scope files.
//!
//! Upstream `resolve_python` only follows leading-dot (relative) imports and
//! treats every absolute `from package.module import Name` / `import
//! package.module` as external. Src-layout monorepos import their own packages
//! absolutely, so those consumer→producer edges were invisible to both
//! dependency engines. This module resolves them, but only within package
//! roots discovered under an **explicit request/worktree scope** — never a scan
//! root inferred from an absolute target path.
//!
//! When one module name maps to more than one in-scope file (duplicate package
//! roots), the identity is reported as ambiguous rather than guessed: the
//! engines then refuse (`tilth_deps`) or degrade to partial (`fetch_dependencies`)
//! instead of returning a confident but wrong edge.

use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

use crate::lang::detect_file_type;
use crate::lang::outline::outline_language;
use crate::types::{FileType, Lang};

/// A resolved import edge with the evidence that produced it.
pub(crate) struct ImportEdge {
    pub(crate) path: PathBuf,
    pub(crate) module: String,
    pub(crate) line: u32,
}

/// An ambiguous *target* module identity: the file's own dotted module name maps
/// to more than one in-scope file, so its dependent set cannot be trusted.
pub(crate) struct AmbiguousTarget {
    pub(crate) module: String,
    pub(crate) candidates: Vec<PathBuf>,
}

/// Result of resolving one Python file's imports against a scope's roots.
/// A module with multiple in-scope candidates (ambiguous) is omitted from
/// `edges` rather than guessed; the [`target_ambiguity`] check is how the
/// engines surface that ambiguity to the caller.
#[derive(Default)]
pub(crate) struct PyResolution {
    pub(crate) edges: Vec<ImportEdge>,
}

impl PyResolution {
    /// Resolved edge paths only — what the persistent index and forward-import
    /// listing consume.
    pub(crate) fn paths(self) -> Vec<PathBuf> {
        self.edges.into_iter().map(|e| e.path).collect()
    }
}

/// Src-layout package roots discovered under one explicit scope.
///
/// Supported layouts: `<scope>/src`, `<scope>/<project>/src`, and
/// `<scope>/packages/<project>/src`. A module `a.b.c` resolves to
/// `<root>/a/b/c.py` or `<root>/a/b/c/__init__.py` under any root.
pub(crate) struct PyRoots {
    roots: Vec<PathBuf>,
}

impl PyRoots {
    /// Enumerate the package roots under `scope`. Directory listings honor the
    /// shared walker skip rules, so `.venv`, `node_modules`, and nested
    /// checkouts never contribute a root.
    pub(crate) fn discover(scope: &Path) -> Self {
        let mut roots = Vec::new();
        push_if_dir(&mut roots, scope.join("src"));
        for project in child_dirs(scope) {
            push_if_dir(&mut roots, project.join("src"));
        }
        for project in child_dirs(&scope.join("packages")) {
            push_if_dir(&mut roots, project.join("src"));
        }
        roots.sort();
        roots.dedup();
        Self { roots }
    }

    /// Every in-scope file a module name maps to, sorted and deduplicated. Zero
    /// means external, one is a unique edge, more than one is ambiguous.
    fn candidates(&self, module: &str) -> Vec<PathBuf> {
        if module.is_empty() {
            return Vec::new();
        }
        let rel: PathBuf = module.split('.').collect();
        let mut hits = Vec::new();
        for root in &self.roots {
            let base = root.join(&rel);
            let as_file = base.with_extension("py");
            if as_file.is_file() {
                hits.push(super::normalize_path(&as_file));
            }
            let as_pkg = base.join("__init__.py");
            if as_pkg.is_file() {
                hits.push(super::normalize_path(&as_pkg));
            }
        }
        hits.sort();
        hits.dedup();
        hits
    }

    /// The dotted module name a file would be imported as, relative to its
    /// containing root. `None` when the file is under no discovered root.
    fn module_of(&self, path: &Path) -> Option<String> {
        let canonical = path.canonicalize().ok()?;
        for root in &self.roots {
            let Ok(root_canon) = root.canonicalize() else {
                continue;
            };
            if let Ok(rel) = canonical.strip_prefix(&root_canon) {
                return module_from_rel(rel);
            }
        }
        None
    }
}

/// Resolve one Python file's imports against `roots`.
///
/// Relative imports reuse the existing dot-resolver unchanged. Absolute imports
/// resolve against the package roots; a module with multiple in-scope
/// candidates is recorded as ambiguous instead of being resolved.
pub(crate) fn resolve_python_edges(
    file_path: &Path,
    content: &str,
    roots: &PyRoots,
) -> PyResolution {
    let mut res = PyResolution::default();
    let Some(dir) = file_path.parent() else {
        return res;
    };
    let Some(tree) = parse_python(content) else {
        return res;
    };
    let mut raw = Vec::new();
    collect_imports(tree.root_node(), content.as_bytes(), &mut raw);

    let mut seen: HashSet<PathBuf> = HashSet::new();
    for (module, line) in raw {
        if res.edges.len() >= super::MAX_SUGGESTIONS {
            break;
        }
        if module.is_empty() {
            continue;
        }
        if module.starts_with('.') {
            // Relative import: existing resolver owns the dot arithmetic and
            // normalization, keeping ordinary package layouts unchanged.
            if let Some(path) = super::resolve(dir, &module, Lang::Python) {
                if seen.insert(path.clone()) {
                    res.edges.push(ImportEdge { path, module, line });
                }
            }
            continue;
        }
        // Exactly one in-scope candidate is a proven edge; zero is external and
        // more than one is ambiguous — both omitted here, never guessed.
        let mut candidates = roots.candidates(&module);
        if candidates.len() == 1 {
            let path = candidates.pop().unwrap();
            if seen.insert(path.clone()) {
                res.edges.push(ImportEdge { path, module, line });
            }
        }
    }
    res
}

/// The ambiguous identity of a Python target, when its own dotted module name
/// maps to more than one in-scope file (duplicate package roots). `None` means
/// the identity is unique (or the target is not Python / not under a root) — the
/// honest signal both engines key their ambiguity behavior on.
pub(crate) fn target_ambiguity(target: &Path, scope: &Path) -> Option<AmbiguousTarget> {
    if !matches!(detect_file_type(target), FileType::Code(Lang::Python)) {
        return None;
    }
    let roots = PyRoots::discover(scope);
    let module = roots.module_of(target)?;
    let candidates = roots.candidates(&module);
    if candidates.len() > 1 {
        Some(AmbiguousTarget { module, candidates })
    } else {
        None
    }
}

fn parse_python(content: &str) -> Option<tree_sitter::Tree> {
    let language = outline_language(Lang::Python)?;
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&language).ok()?;
    parser.parse(content, None)
}

/// Walk the tree collecting `(module_source, line)` for every import statement,
/// using the AST so aliases and parenthesized from-imports resolve correctly.
fn collect_imports(node: tree_sitter::Node, src: &[u8], out: &mut Vec<(String, u32)>) {
    match node.kind() {
        "import_from_statement" => {
            if let Some(module) = node.child_by_field_name("module_name") {
                if let Ok(text) = module.utf8_text(src) {
                    out.push((text.to_string(), module.start_position().row as u32 + 1));
                }
            }
        }
        "import_statement" => {
            let mut cursor = node.walk();
            for name in node.children_by_field_name("name", &mut cursor) {
                // `import a.b as c` — the module is the `name` field of the
                // aliased_import; a bare `import a.b` is a dotted_name directly.
                let dotted = if name.kind() == "aliased_import" {
                    name.child_by_field_name("name")
                } else {
                    Some(name)
                };
                if let Some(dotted) = dotted {
                    if let Ok(text) = dotted.utf8_text(src) {
                        out.push((text.to_string(), dotted.start_position().row as u32 + 1));
                    }
                }
            }
        }
        _ => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_imports(child, src, out);
            }
        }
    }
}

/// Convert a root-relative `.py` / `__init__.py` path to its dotted module name.
fn module_from_rel(rel: &Path) -> Option<String> {
    let mut segments: Vec<String> = rel
        .components()
        .filter_map(|c| match c {
            Component::Normal(s) => s.to_str().map(str::to_string),
            _ => None,
        })
        .collect();
    let last = segments.last()?.clone();
    if last == "__init__.py" {
        segments.pop();
    } else {
        let stem = last.strip_suffix(".py")?;
        let idx = segments.len() - 1;
        segments[idx] = stem.to_string();
    }
    if segments.is_empty() {
        return None;
    }
    Some(segments.join("."))
}

fn push_if_dir(roots: &mut Vec<PathBuf>, candidate: PathBuf) {
    if candidate.is_dir() {
        roots.push(candidate);
    }
}

fn child_dirs(dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|e| e.file_type().is_ok_and(|ft| ft.is_dir()))
        .filter(|e| {
            e.file_name()
                .to_str()
                .is_some_and(|name| !crate::search::skip_dir_entry(&e.path(), name))
        })
        .map(|e| e.path())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// The confirmed src-layout fixture: a producer package under `packages/`
    /// and a consumer package under a top-level project dir.
    fn producer_consumer() -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        for (rel, body) in [
            ("packages/producer/src/producer/__init__.py", ""),
            (
                "packages/producer/src/producer/ingest/rankings.py",
                "class RankingEntry:\n    pass\n",
            ),
            (
                "packages/producer/src/producer/ingest/__init__.py",
                "from .rankings import RankingEntry\n",
            ),
            ("consumer/src/consumer/__init__.py", ""),
        ] {
            let path = root.join(rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, body).unwrap();
        }
        tmp
    }

    fn write(root: &Path, rel: &str, body: &str) -> PathBuf {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, body).unwrap();
        path
    }

    #[test]
    fn discovers_src_layout_roots() {
        let tmp = producer_consumer();
        let roots = PyRoots::discover(tmp.path());
        assert!(roots
            .roots
            .contains(&tmp.path().join("packages/producer/src")));
        assert!(roots.roots.contains(&tmp.path().join("consumer/src")));
    }

    #[test]
    fn absolute_from_import_resolves_to_unique_module_file() {
        let tmp = producer_consumer();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/direct.py",
            "from producer.ingest.rankings import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let edges =
            resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots).edges;
        assert_eq!(edges.len(), 1);
        assert_eq!(
            edges[0].path,
            tmp.path()
                .join("packages/producer/src/producer/ingest/rankings.py")
        );
        assert_eq!(edges[0].module, "producer.ingest.rankings");
        assert_eq!(edges[0].line, 1);
    }

    #[test]
    fn barrel_from_import_resolves_to_package_init() {
        let tmp = producer_consumer();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/surface.py",
            "from producer.ingest import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let edges =
            resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots).edges;
        assert_eq!(
            edges.iter().map(|e| &e.path).collect::<Vec<_>>(),
            vec![&tmp
                .path()
                .join("packages/producer/src/producer/ingest/__init__.py")]
        );
    }

    #[test]
    fn aliased_plain_import_resolves_module_not_alias() {
        let tmp = producer_consumer();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/aliased.py",
            "import producer.ingest.rankings as r\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let edges =
            resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots).edges;
        assert_eq!(edges.len(), 1);
        assert_eq!(edges[0].module, "producer.ingest.rankings");
    }

    #[test]
    fn relative_import_is_preserved() {
        let tmp = producer_consumer();
        let init = tmp
            .path()
            .join("packages/producer/src/producer/ingest/__init__.py");
        let roots = PyRoots::discover(tmp.path());
        let edges = resolve_python_edges(&init, &fs::read_to_string(&init).unwrap(), &roots).edges;
        assert_eq!(
            edges.iter().map(|e| &e.path).collect::<Vec<_>>(),
            vec![&tmp
                .path()
                .join("packages/producer/src/producer/ingest/rankings.py")]
        );
    }

    #[test]
    fn same_symbol_name_in_other_module_resolves_elsewhere() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/other/src/other/rankings.py",
            "class RankingEntry:\n    pass\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/unrelated.py",
            "from other.rankings import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let edges =
            resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots).edges;
        assert_eq!(
            edges.iter().map(|e| &e.path).collect::<Vec<_>>(),
            vec![&tmp.path().join("packages/other/src/other/rankings.py")]
        );
    }

    #[test]
    fn duplicate_roots_make_target_identity_ambiguous() {
        let tmp = producer_consumer();
        // A second producer/ingest/rankings.py under <scope>/src.
        let dup = write(
            tmp.path(),
            "src/producer/ingest/rankings.py",
            "class RankingEntry:\n    pass\n",
        );
        let target = tmp
            .path()
            .join("packages/producer/src/producer/ingest/rankings.py");
        let ambiguous = target_ambiguity(&target, tmp.path()).expect("should be ambiguous");
        assert_eq!(ambiguous.module, "producer.ingest.rankings");
        assert_eq!(ambiguous.candidates.len(), 2);
        assert!(ambiguous.candidates.iter().any(|c| c == &dup));
    }

    #[test]
    fn unique_target_identity_is_not_ambiguous() {
        let tmp = producer_consumer();
        let target = tmp
            .path()
            .join("packages/producer/src/producer/ingest/rankings.py");
        assert!(target_ambiguity(&target, tmp.path()).is_none());
    }

    #[test]
    fn non_python_target_is_never_ambiguous() {
        let tmp = tempfile::tempdir().unwrap();
        let target = write(tmp.path(), "src/foo.ts", "export const x = 1;\n");
        assert!(target_ambiguity(&target, tmp.path()).is_none());
    }
}
