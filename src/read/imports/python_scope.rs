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

use std::collections::{HashMap, HashSet};
use std::path::{Component, Path, PathBuf};
use std::rc::Rc;

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

/// Why one import could not become a proven edge: never guessed, always surfaced.
pub(crate) enum Uncertainty {
    /// The imported module itself has more than one in-scope candidate.
    AmbiguousModule {
        module: String,
        candidates: Vec<PathBuf>,
    },
    /// A named re-export chain could not settle on one owner (duplicate
    /// owners, a blocked ambiguous hop, or a cycle) — see `OwnerResult::Blocked`.
    BlockedOwnership { module: String, name: String },
    /// A package initializer on the re-export chain could not be read or
    /// parsed, so the evidence is incomplete. This is not a missing export.
    UnavailableOwner {
        module: String,
        name: String,
        path: PathBuf,
    },
}

/// Result of resolving one Python file's imports against a scope's roots.
/// A module with multiple in-scope candidates (ambiguous) is omitted from
/// `edges` rather than guessed; the [`target_ambiguity`] check is how the
/// engines surface the *target's own* identity ambiguity, while `uncertain`
/// carries this file's own unresolved forward imports.
#[derive(Default)]
pub(crate) struct PyResolution {
    pub(crate) edges: Vec<ImportEdge>,
    pub(crate) uncertain: Vec<Uncertainty>,
    /// Every intermediate `__init__.py` visited while proving a named
    /// re-export's owner (`OwnerResult::Owned` only) — a superset of what's in
    /// `edges` for chains longer than one hop. Consumed only by the persistent
    /// index's invalidation pass (`reconcile`), which needs to find a consumer
    /// whose resolution passed through a hop that isn't itself a stored
    /// direct/owner edge (a "redirect"). Never part of the lean forward-edge
    /// contract `paths()`/`edges` gives ordinary callers (`impact`, `tilth_deps`
    /// presentation) — do not surface this anywhere else.
    pub(crate) reexport_hops: Vec<PathBuf>,
}

impl PyResolution {
    /// Resolved edge paths only — what the persistent index and forward-import
    /// listing consume.
    pub(crate) fn paths(self) -> Vec<PathBuf> {
        self.edges.into_iter().map(|e| e.path).collect()
    }

    /// The fields `reconcile`'s shard builder stores: forward edge paths,
    /// re-export chain hops, and whether any import stayed unresolved. The
    /// uncertainty flag keeps an incomplete edge set from being stored as a
    /// complete one.
    pub(crate) fn into_shard_fields(self) -> ScopedShardFields {
        ScopedShardFields {
            uncertain: !self.uncertain.is_empty(),
            paths: self.edges.into_iter().map(|e| e.path).collect(),
            hops: self.reexport_hops,
        }
    }
}

/// What the persistent deps index stores for one file.
#[derive(Default)]
pub(crate) struct ScopedShardFields {
    pub(crate) paths: Vec<PathBuf>,
    pub(crate) hops: Vec<PathBuf>,
    /// At least one import could not become a proven edge.
    pub(crate) uncertain: bool,
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
    /// checkouts never contribute a root. A candidate root whose canonical
    /// path leaves the canonical scope (a symlink to another checkout) is
    /// rejected, so scoped resolution never reads modules outside the
    /// requested scope.
    pub(crate) fn discover(scope: &Path) -> Self {
        let Ok(scope_canon) = scope.canonicalize() else {
            return Self { roots: Vec::new() };
        };
        let mut roots = Vec::new();
        push_if_contained_dir(&mut roots, scope.join("src"), &scope_canon);
        for project in child_dirs(scope) {
            push_if_contained_dir(&mut roots, project.join("src"), &scope_canon);
        }
        for project in child_dirs(&scope.join("packages")) {
            push_if_contained_dir(&mut roots, project.join("src"), &scope_canon);
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
    collect_python_imports(tree.root_node(), content.as_bytes(), &mut raw);

    let mut seen: HashSet<PathBuf> = HashSet::new();
    // One initializer cache per resolution pass: each `__init__.py` on a
    // re-export chain is read and parsed once, not once per (name, hop).
    let mut inits = InitCache::default();
    for imp in raw {
        if imp.module.is_empty() {
            continue;
        }
        // The imported module itself: a direct edge when it resolves to exactly
        // one in-scope file. Zero candidates is external (silently skipped, not
        // uncertainty) and more than one is ambiguous — surfaced, never guessed.
        // Relative modules reuse the existing dot-resolver, keeping ordinary
        // package layouts unchanged.
        let direct = match resolve_module_file(&imp.module, dir, roots) {
            ModuleTarget::File(direct) => direct,
            ModuleTarget::Ambiguous => {
                res.uncertain.push(Uncertainty::AmbiguousModule {
                    module: imp.module.clone(),
                    candidates: roots.candidates(&imp.module),
                });
                continue;
            }
            ModuleTarget::External => continue,
        };
        if seen.insert(direct.clone()) {
            res.edges.push(ImportEdge {
                path: direct.clone(),
                module: imp.module.clone(),
                line: imp.line,
            });
        }
        // Named re-export ownership: when the imported module is a package
        // `__init__.py` that explicitly re-exports the requested name, also
        // associate this consumer with the file that defines it. Only the
        // specific imported name is followed, so importing one public name never
        // attaches the consumer to every file the package re-exports.
        if !is_init_py(&direct) {
            continue;
        }
        for nm in &imp.names {
            let mut visited = HashSet::new();
            let outcome =
                resolve_reexport_owner(&direct, &nm.original, roots, &mut visited, &mut inits);
            res.reexport_hops
                .extend(visited.iter().map(|(path, _)| path.clone()));
            match outcome {
                OwnerResult::Owned(owner) => {
                    if owner != direct && seen.insert(owner.clone()) {
                        res.edges.push(ImportEdge {
                            path: owner,
                            module: imp.module.clone(),
                            line: imp.line,
                        });
                    }
                }
                OwnerResult::Blocked => {
                    res.uncertain.push(Uncertainty::BlockedOwnership {
                        module: imp.module.clone(),
                        name: nm.original.clone(),
                    });
                }
                OwnerResult::Unavailable(path) => {
                    res.uncertain.push(Uncertainty::UnavailableOwner {
                        module: imp.module.clone(),
                        name: nm.original.clone(),
                        path,
                    });
                }
                OwnerResult::External | OwnerResult::NotFound => {}
            }
        }
    }
    res.reexport_hops.sort();
    res.reexport_hops.dedup();
    res
}

/// The ambiguous identity of a Python target, when its own dotted module name
/// maps to more than one in-scope file (duplicate package roots). `None` means
/// the identity is unique (or the target is not Python / not under a root) — the
/// honest signal both engines key their ambiguity behavior on. Callers pass
/// the `roots` they already discovered for the scope.
pub(crate) fn target_ambiguity(target: &Path, roots: &PyRoots) -> Option<AmbiguousTarget> {
    if !matches!(detect_file_type(target), FileType::Code(Lang::Python)) {
        return None;
    }
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

/// One import statement in raw form: the module source, the line it sits on,
/// and — for `from module import ...` — the names it binds. Plain `import a.b`
/// and wildcard `from a import *` carry no traceable names.
#[derive(Clone)]
struct RawImport {
    module: String,
    line: u32,
    names: Vec<ImportedName>,
}

/// One name bound by a `from` import. `local` is how the importing file refers
/// to it (the alias when present); `original` is the name in the source module,
/// which is the name the re-export chain is followed by.
#[derive(Clone)]
struct ImportedName {
    local: String,
    original: String,
}

/// Walk the tree collecting one [`RawImport`] per import statement, using the
/// AST so aliases and parenthesized from-imports resolve correctly.
fn collect_python_imports(node: tree_sitter::Node, src: &[u8], out: &mut Vec<RawImport>) {
    match node.kind() {
        "import_from_statement" => {
            let Some(module) = node.child_by_field_name("module_name") else {
                return;
            };
            let Ok(module_text) = module.utf8_text(src) else {
                return;
            };
            let mut names = Vec::new();
            let mut cursor = node.walk();
            for name in node.children_by_field_name("name", &mut cursor) {
                if let Some(imported) = imported_name(name, src) {
                    names.push(imported);
                }
            }
            out.push(RawImport {
                module: module_text.to_string(),
                line: module.start_position().row as u32 + 1,
                names,
            });
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
                        out.push(RawImport {
                            module: text.to_string(),
                            line: dotted.start_position().row as u32 + 1,
                            names: Vec::new(),
                        });
                    }
                }
            }
        }
        _ => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_python_imports(child, src, out);
            }
        }
    }
}

/// The `(local, original)` pair for one `from ... import` name, or `None` for a
/// wildcard (`*`), which binds no traceable name.
fn imported_name(node: tree_sitter::Node, src: &[u8]) -> Option<ImportedName> {
    if node.kind() == "aliased_import" {
        let original = node.child_by_field_name("name")?.utf8_text(src).ok()?;
        let local = node.child_by_field_name("alias")?.utf8_text(src).ok()?;
        Some(ImportedName {
            local: local.to_string(),
            original: original.to_string(),
        })
    } else if matches!(node.kind(), "dotted_name" | "identifier") {
        let text = node.utf8_text(src).ok()?;
        Some(ImportedName {
            local: text.to_string(),
            original: text.to_string(),
        })
    } else {
        // wildcard_import and any other node bind no traceable name.
        None
    }
}

/// Whether a path is a Python package initializer (`__init__.py`). The one
/// crate-private predicate for this check; the persistent deps index reuses it.
pub(crate) fn is_init_py(path: &Path) -> bool {
    path.file_name().and_then(|n| n.to_str()) == Some("__init__.py")
}

/// Where a module reference resolves within scope.
enum ModuleTarget {
    /// A unique in-scope file.
    File(PathBuf),
    /// Multiple in-scope candidates (duplicate roots): identity is unreliable.
    Ambiguous,
    /// No in-scope candidate: the module is external to this scope.
    External,
}

/// Resolve one module reference — relative (`.rankings`) or absolute
/// (`producer.ingest`) — to its in-scope file. Relative references reuse the
/// existing dot-resolver; absolute references go through the package roots.
fn resolve_module_file(module: &str, dir: &Path, roots: &PyRoots) -> ModuleTarget {
    if module.starts_with('.') {
        match super::resolve(dir, module, Lang::Python) {
            Some(path) => ModuleTarget::File(path),
            None => ModuleTarget::External,
        }
    } else {
        let mut candidates = roots.candidates(module);
        match candidates.len() {
            0 => ModuleTarget::External,
            1 => ModuleTarget::File(candidates.pop().unwrap()),
            _ => ModuleTarget::Ambiguous,
        }
    }
}

/// Outcome of following a name through explicit re-export bindings.
enum OwnerResult {
    /// A single defining file proven by the binding chain.
    Owned(PathBuf),
    /// The name is not re-exported here.
    NotFound,
    /// The name comes from a module outside the current scope.
    External,
    /// A cycle or an ambiguous owner was hit; no owner is fabricated.
    Blocked,
    /// An initializer on the chain could not be read or parsed. The evidence
    /// is incomplete, which is different from a name that is not re-exported.
    Unavailable(PathBuf),
}

/// One parsed package initializer: its source, syntax tree, and imports.
struct ParsedInit {
    content: String,
    tree: tree_sitter::Tree,
    imports: Vec<RawImport>,
}

/// Parsed initializers for one resolution pass. `None` records a read or
/// parse failure so the failure is also reported once, not retried per hop.
#[derive(Default)]
struct InitCache {
    files: HashMap<PathBuf, Option<Rc<ParsedInit>>>,
}

impl InitCache {
    fn get(&mut self, init: &Path) -> Option<Rc<ParsedInit>> {
        if let Some(cached) = self.files.get(init) {
            return cached.clone();
        }
        let parsed = std::fs::read_to_string(init).ok().and_then(|content| {
            let tree = parse_python(&content)?;
            let mut imports = Vec::new();
            collect_python_imports(tree.root_node(), content.as_bytes(), &mut imports);
            Some(Rc::new(ParsedInit {
                content,
                tree,
                imports,
            }))
        });
        self.files.insert(init.to_path_buf(), parsed.clone());
        parsed
    }
}

fn initializer_defines_name(inits: &mut InitCache, init: &Path, name: &str) -> bool {
    let Some(parsed) = inits.get(init) else {
        return false;
    };
    let root = parsed.tree.root_node();
    let mut cursor = root.walk();
    for node in root.named_children(&mut cursor) {
        if python_node_defines_name(node, parsed.content.as_bytes(), name) {
            return true;
        }
    }
    false
}

fn python_node_defines_name(node: tree_sitter::Node, src: &[u8], name: &str) -> bool {
    let kind = node.kind();
    if kind == "class_definition" || kind == "function_definition" || kind == "type_alias_statement"
    {
        return node
            .child_by_field_name("name")
            .and_then(|node| node.utf8_text(src).ok())
            == Some(name);
    }
    if kind == "decorated_definition" || kind == "expression_statement" {
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            if python_node_defines_name(child, src, name) {
                return true;
            }
        }
        return false;
    }
    if kind == "assignment" {
        let Some(left) = node.child_by_field_name("left") else {
            return false;
        };
        return python_node_defines_name(left, src, name);
    }
    if kind == "identifier" {
        return node.utf8_text(src).ok() == Some(name);
    }
    if kind == "list_pattern" || kind == "tuple_pattern" || kind == "pattern_list" {
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            if python_node_defines_name(child, src, name) {
                return true;
            }
        }
    }
    false
}

/// Follow explicit `from ... import name` bindings in `init` (an `__init__.py`)
/// to the single file that defines `name`. Relative and absolute re-export
/// modules, aliases, and parenthesized lists are all supported. A `(file, name)`
/// visited set makes cycles terminate; two bindings of the same name to
/// different files are ambiguous and resolve to no owner rather than by order.
/// An initializer that cannot be read or parsed makes the chain `Unavailable`
/// so callers do not mistake incomplete evidence for a missing export.
fn resolve_reexport_owner(
    init: &Path,
    name: &str,
    roots: &PyRoots,
    visited: &mut HashSet<(PathBuf, String)>,
    inits: &mut InitCache,
) -> OwnerResult {
    if !visited.insert((init.to_path_buf(), name.to_string())) {
        return OwnerResult::Blocked; // cycle: terminate without fabricating an owner
    }
    let Some(parsed) = inits.get(init) else {
        return OwnerResult::Unavailable(init.to_path_buf());
    };
    let Some(dir) = init.parent() else {
        return OwnerResult::Unavailable(init.to_path_buf());
    };

    let mut owners: Vec<PathBuf> = Vec::new();
    let mut blocked = false;
    let mut external = false;
    let mut unavailable: Option<PathBuf> = None;
    for imp in &parsed.imports {
        for nm in imp.names.iter().filter(|nm| nm.local == name) {
            match resolve_module_file(&imp.module, dir, roots) {
                ModuleTarget::Ambiguous => blocked = true,
                ModuleTarget::External => external = true,
                ModuleTarget::File(target) => {
                    if is_init_py(&target) {
                        match resolve_reexport_owner(&target, &nm.original, roots, visited, inits) {
                            OwnerResult::Owned(f) => owners.push(f),
                            OwnerResult::NotFound => {
                                if initializer_defines_name(inits, &target, &nm.original) {
                                    owners.push(target);
                                }
                            }
                            OwnerResult::External => external = true,
                            OwnerResult::Blocked => blocked = true,
                            OwnerResult::Unavailable(path) => unavailable = Some(path),
                        }
                    } else {
                        owners.push(target); // leaf module defines the name
                    }
                }
            }
        }
    }
    owners.sort();
    owners.dedup();
    // Incomplete evidence is reported before any verdict on the owners.
    if let Some(path) = unavailable {
        return OwnerResult::Unavailable(path);
    }
    // A duplicate owner or any blocked hop makes the identity unreliable: never
    // resolve by traversal order, and never fabricate an owner.
    if owners.len() > 1 || blocked || (external && !owners.is_empty()) {
        return OwnerResult::Blocked;
    }
    if external {
        return OwnerResult::External;
    }
    match owners.pop() {
        Some(owner) => OwnerResult::Owned(owner),
        None => OwnerResult::NotFound,
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

/// Push `candidate` as a root only when it is a directory whose canonical
/// path stays inside the canonical scope. The scope-form path is kept so
/// resolved edges keep the caller's spelling.
fn push_if_contained_dir(roots: &mut Vec<PathBuf>, candidate: PathBuf, scope_canon: &Path) {
    if !candidate.is_dir() {
        return;
    }
    let Ok(canon) = candidate.canonicalize() else {
        return;
    };
    if canon.starts_with(scope_canon) {
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

    /// The resolved edge paths for one consumer file, sorted for comparison.
    fn edge_paths(root: &Path, consumer: &Path) -> Vec<PathBuf> {
        let roots = PyRoots::discover(root);
        let mut paths: Vec<PathBuf> =
            resolve_python_edges(consumer, &fs::read_to_string(consumer).unwrap(), &roots)
                .edges
                .into_iter()
                .map(|e| e.path)
                .collect();
        paths.sort();
        paths
    }

    #[test]
    fn barrel_from_import_keeps_surface_and_adds_reexport_owner() {
        let tmp = producer_consumer();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/surface.py",
            "from producer.ingest import RankingEntry\n",
        );
        // The direct edge to the package surface is preserved, and the proven
        // defining file is added — both, never a substitution.
        let mut expected = vec![
            tmp.path()
                .join("packages/producer/src/producer/ingest/__init__.py"),
            tmp.path()
                .join("packages/producer/src/producer/ingest/rankings.py"),
        ];
        expected.sort();
        assert_eq!(edge_paths(tmp.path(), &consumer), expected);
    }

    #[test]
    fn reexport_owner_honors_public_and_consumer_aliases() {
        let tmp = producer_consumer();
        // Public alias in the package surface: `RankingEntry as Ranked`.
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry as Ranked\n",
        );
        // Consumer alias on top of the public alias.
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/aliased_surface.py",
            "from producer.ingest import Ranked as R\n",
        );
        assert!(edge_paths(tmp.path(), &consumer).contains(
            &tmp.path()
                .join("packages/producer/src/producer/ingest/rankings.py")
        ));
    }

    #[test]
    fn reexport_owner_follows_parenthesized_list() {
        let tmp = producer_consumer();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/parened.py",
            "from producer.ingest import (\n    RankingEntry,\n)\n",
        );
        assert!(edge_paths(tmp.path(), &consumer).contains(
            &tmp.path()
                .join("packages/producer/src/producer/ingest/rankings.py")
        ));
    }

    #[test]
    fn reexport_owner_follows_two_hop_init_chain() {
        let tmp = producer_consumer();
        // producer/__init__ re-exports from the ingest subpackage, which in turn
        // re-exports from rankings.py: a two-hop __init__ chain.
        write(
            tmp.path(),
            "packages/producer/src/producer/__init__.py",
            "from .ingest import RankingEntry\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/two_hop.py",
            "from producer import RankingEntry\n",
        );
        assert!(edge_paths(tmp.path(), &consumer).contains(
            &tmp.path()
                .join("packages/producer/src/producer/ingest/rankings.py")
        ));
    }

    #[test]
    fn reexport_owner_preserves_external_chain_result() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/__init__.py",
            "from .ingest import RankingEntry\n",
        );
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from external_package import RankingEntry\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/external_chain.py",
            "from producer import RankingEntry\n",
        );
        assert_eq!(
            edge_paths(tmp.path(), &consumer),
            vec![tmp
                .path()
                .join("packages/producer/src/producer/__init__.py")]
        );
    }

    #[test]
    fn reexport_owner_accepts_name_defined_in_nested_init() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/__init__.py",
            "from .ingest import RankingEntry\n",
        );
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "class RankingEntry:\n    pass\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/local_chain.py",
            "from producer import RankingEntry\n",
        );
        let paths = edge_paths(tmp.path(), &consumer);
        assert!(paths.contains(
            &tmp.path()
                .join("packages/producer/src/producer/ingest/__init__.py")
        ));
    }

    #[test]
    fn reexport_owner_isolates_each_public_name() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/models.py",
            "class Model:\n    pass\n",
        );
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry\nfrom .models import Model\n",
        );
        // A consumer of Model must not become a dependent of rankings.py.
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/model_user.py",
            "from producer.ingest import Model\n",
        );
        let paths = edge_paths(tmp.path(), &consumer);
        assert!(paths.contains(
            &tmp.path()
                .join("packages/producer/src/producer/ingest/models.py")
        ));
        assert!(!paths.contains(
            &tmp.path()
                .join("packages/producer/src/producer/ingest/rankings.py")
        ));
    }

    #[test]
    fn duplicate_public_name_owner_is_not_resolved_by_order() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/other.py",
            "class RankingEntry:\n    pass\n",
        );
        // Two bindings of the same public name to different files: ambiguous.
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry\nfrom .other import RankingEntry\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/dup.py",
            "from producer.ingest import RankingEntry\n",
        );
        // Only the direct edge to the package surface survives; neither owner is
        // guessed by traversal order.
        assert_eq!(
            edge_paths(tmp.path(), &consumer),
            vec![tmp
                .path()
                .join("packages/producer/src/producer/ingest/__init__.py")]
        );
    }

    #[test]
    fn cyclic_reexport_terminates_without_fabricating_owner() {
        let tmp = producer_consumer();
        // ingest/__init__ re-exports X from itself: a self-referential cycle.
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from . import RankingEntry\n",
        );
        let init = tmp
            .path()
            .join("packages/producer/src/producer/ingest/__init__.py");
        let roots = PyRoots::discover(tmp.path());
        let mut visited = HashSet::new();
        let mut inits = InitCache::default();
        // The resolver terminates and reports no fabricated owner.
        assert!(matches!(
            resolve_reexport_owner(&init, "RankingEntry", &roots, &mut visited, &mut inits),
            OwnerResult::Blocked
        ));
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
        let roots = PyRoots::discover(tmp.path());
        let ambiguous = target_ambiguity(&target, &roots).expect("should be ambiguous");
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
        assert!(target_ambiguity(&target, &PyRoots::discover(tmp.path())).is_none());
    }

    #[test]
    fn non_python_target_is_never_ambiguous() {
        let tmp = tempfile::tempdir().unwrap();
        let target = write(tmp.path(), "src/foo.ts", "export const x = 1;\n");
        assert!(target_ambiguity(&target, &PyRoots::discover(tmp.path())).is_none());
    }

    #[test]
    fn ambiguous_direct_module_import_is_surfaced_as_uncertainty() {
        let tmp = producer_consumer();
        // A second producer/ingest/rankings.py under <scope>/src makes the
        // consumer's own direct import ambiguous.
        write(
            tmp.path(),
            "src/producer/ingest/rankings.py",
            "class RankingEntry:\n    pass\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/ambiguous_direct.py",
            "from producer.ingest.rankings import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let res = resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots);
        assert!(res.edges.is_empty());
        assert_eq!(res.uncertain.len(), 1);
        assert!(matches!(
            &res.uncertain[0],
            Uncertainty::AmbiguousModule { module, candidates }
                if module == "producer.ingest.rankings" && candidates.len() == 2
        ));
    }

    #[test]
    fn duplicate_named_owner_is_surfaced_as_blocked_ownership() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/other.py",
            "class RankingEntry:\n    pass\n",
        );
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .rankings import RankingEntry\nfrom .other import RankingEntry\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/dup_owner.py",
            "from producer.ingest import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let res = resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots);
        assert_eq!(res.edges.len(), 1); // the direct edge to the package surface
        assert_eq!(res.uncertain.len(), 1);
        assert!(matches!(
            &res.uncertain[0],
            Uncertainty::BlockedOwnership { module, name }
                if module == "producer.ingest" && name == "RankingEntry"
        ));
    }

    #[test]
    fn cyclic_reexport_is_surfaced_as_blocked_ownership() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from . import RankingEntry\n",
        );
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/cyclic.py",
            "from producer.ingest import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let res = resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots);
        assert_eq!(res.edges.len(), 1); // the direct edge to the package surface
        assert_eq!(res.uncertain.len(), 1);
        assert!(matches!(
            &res.uncertain[0],
            Uncertainty::BlockedOwnership { module, name }
                if module == "producer.ingest" && name == "RankingEntry"
        ));
    }

    #[test]
    fn nine_distinct_local_modules_all_become_edges() {
        use std::fmt::Write as _;
        let tmp = producer_consumer();
        let mut import_lines = String::new();
        for i in 0..9 {
            write(
                tmp.path(),
                &format!("packages/producer/src/producer/mod{i}.py"),
                "x = 1\n",
            );
            let _ = writeln!(import_lines, "import producer.mod{i}");
        }
        let consumer = write(tmp.path(), "consumer/src/consumer/nine.py", &import_lines);
        let roots = PyRoots::discover(tmp.path());
        let res = resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots);
        assert_eq!(res.edges.len(), 9);
        assert!(res.uncertain.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_src_outside_scope_is_never_a_root() {
        let outside = tempfile::tempdir().unwrap();
        write(outside.path(), "src/secret/__init__.py", "TOKEN = 1\n");
        let scope = tempfile::tempdir().unwrap();
        fs::create_dir_all(scope.path().join("proj")).unwrap();
        fs::create_dir_all(scope.path().join("packages/pkg")).unwrap();
        for link in ["src", "proj/src", "packages/pkg/src"] {
            std::os::unix::fs::symlink(outside.path().join("src"), scope.path().join(link))
                .unwrap();
        }
        let consumer = write(scope.path(), "app.py", "import secret\n");

        let roots = PyRoots::discover(scope.path());
        assert!(roots.roots.is_empty(), "escaped roots: {:?}", roots.roots);
        let res = resolve_python_edges(&consumer, "import secret\n", &roots);
        assert!(res.edges.is_empty(), "scoped resolution left the scope");
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_src_inside_scope_stays_a_root() {
        let tmp = tempfile::tempdir().unwrap();
        write(tmp.path(), "real/lib/pkg/__init__.py", "");
        std::os::unix::fs::symlink(tmp.path().join("real/lib"), tmp.path().join("src")).unwrap();
        let roots = PyRoots::discover(tmp.path());
        assert_eq!(roots.roots, vec![tmp.path().join("src")]);
        assert_eq!(roots.candidates("pkg").len(), 1);
    }

    #[test]
    fn unreadable_initializer_is_unavailable_not_a_missing_export() {
        let tmp = producer_consumer();
        write(
            tmp.path(),
            "packages/producer/src/producer/ingest/__init__.py",
            "from .sub import RankingEntry\n",
        );
        // Invalid UTF-8 makes the nested initializer unreadable as text.
        let sub = tmp
            .path()
            .join("packages/producer/src/producer/ingest/sub/__init__.py");
        fs::create_dir_all(sub.parent().unwrap()).unwrap();
        fs::write(&sub, [0xff, 0xfe, 0x00]).unwrap();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/unavailable.py",
            "from producer.ingest import RankingEntry\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let res = resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots);
        // The proven direct edge stays; the owner is reported as unavailable.
        assert_eq!(res.edges.len(), 1);
        assert!(
            matches!(
                res.uncertain.as_slice(),
                [Uncertainty::UnavailableOwner { module, name, path }]
                    if module == "producer.ingest" && name == "RankingEntry" && path == &sub
            ),
            "expected one unavailable owner"
        );
    }

    #[test]
    fn name_absent_from_readable_initializer_is_not_uncertain() {
        let tmp = producer_consumer();
        let consumer = write(
            tmp.path(),
            "consumer/src/consumer/absent.py",
            "from producer.ingest import NotExported\n",
        );
        let roots = PyRoots::discover(tmp.path());
        let res = resolve_python_edges(&consumer, &fs::read_to_string(&consumer).unwrap(), &roots);
        assert!(res.uncertain.is_empty());
    }

    #[test]
    fn initializer_is_parsed_once_per_resolution_pass() {
        let tmp = producer_consumer();
        let init = tmp
            .path()
            .join("packages/producer/src/producer/ingest/__init__.py");
        let mut inits = InitCache::default();
        let first = inits.get(&init).expect("readable initializer");
        // A later read of the same path returns the cached parse, even after
        // the file changes on disk within the pass.
        fs::write(&init, "").unwrap();
        let second = inits.get(&init).expect("cached initializer");
        assert!(Rc::ptr_eq(&first, &second));
        assert_eq!(second.imports.len(), 1);
    }
}
