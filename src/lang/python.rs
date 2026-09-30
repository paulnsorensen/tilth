//! Python language spec. Diverges on: stdlib first-segment rule (`.` separator).

use crate::lang::spec::{LangSpec, StdlibRule, StripFamily, DEFAULT_DEFS};

const CALLEE_QUERY: &str = concat!(
    "(call function: (identifier) @callee)\n",
    "(call function: (attribute attribute: (identifier) @callee))\n",
);

const SIBLING_QUERY: &str = "(attribute object: (identifier) @obj attribute: (identifier) @ref)\n";

/// Common stdlib modules — not exhaustive, but covers the noisy ones.
const STDLIB_MODULES: &[&str] = &[
    "os",
    "sys",
    "re",
    "json",
    "math",
    "time",
    "datetime",
    "pathlib",
    "typing",
    "collections",
    "functools",
    "itertools",
    "abc",
    "io",
    "logging",
    "unittest",
    "dataclasses",
    "enum",
    "copy",
    "hashlib",
    "subprocess",
    "threading",
    "asyncio",
];

pub(crate) const SPEC: LangSpec = LangSpec {
    display: "Python",
    extensions: &["py", "pyi"],
    filenames: &[],
    grammar: Some(tree_sitter_python::LANGUAGE),
    callee_query: Some(CALLEE_QUERY),
    sibling_query: Some(SIBLING_QUERY),
    stdlib: StdlibRule::PythonSegment(STDLIB_MODULES),
    scoped_imports: true,
    manifests: &["pyproject.toml", "setup.py"],
    has_lifetimes: false,
    strip_family: Some(StripFamily::Python),
    extract_receiver: None,
    definitions: DEFAULT_DEFS,
    definition_wrappers: &["decorated_definition"],
    canonical_anchor,
    attach_leading_adornment,
    policy: crate::lang::spec::LanguagePolicy {
        triple_quoted_strings: true,
        hash_line_comments: true,
        import_line,
        import_external,
        import_resolver: resolve_import,
        sibling_object: Some("self"),
        search_priority: 9,
        search_extensions: &["py"],
        basename_extensions: &["py"],
        test_filename: Some(crate::lang::spec::TestFilenamePolicy {
            order: 2,
            label: "test_*.py",
            matches: is_test_filename,
        }),
        ..crate::lang::spec::DEFAULT_POLICY
    },
    semantic_start: crate::lang::spec::default_semantic_start,
};

pub(crate) fn canonical_anchor(node: tree_sitter::Node) -> tree_sitter::Node {
    crate::lang::spec::keyword_canonical_anchor(node, &["def", "class"])
}

pub(crate) fn attach_leading_adornment(
    adornment: tree_sitter::Node,
    _definition: Option<tree_sitter::Node>,
    _lines: &[&str],
) -> bool {
    adornment.kind() == "decorator"
}

fn import_line(line: &str) -> bool {
    let trimmed = line.trim_start();
    trimmed.starts_with("import ") || trimmed.starts_with("from ")
}

fn import_external(source: &str) -> bool {
    !source.starts_with('.')
}

fn resolve_import(dir: &std::path::Path, source: &str) -> Option<std::path::PathBuf> {
    let dots = source.bytes().take_while(|&byte| byte == b'.').count();
    if dots == 0 {
        return None;
    }
    let mut base = dir.to_path_buf();
    for _ in 1..dots {
        base = base.parent()?.to_path_buf();
    }
    let module = &source[dots..];
    if module.is_empty() {
        let init = base.join("__init__.py");
        return init.exists().then_some(init);
    }
    let relative = module.replace('.', "/");
    let file = base.join(format!("{relative}.py"));
    if file.exists() {
        return Some(file);
    }
    let package = base.join(relative).join("__init__.py");
    package.exists().then_some(package)
}

fn is_test_filename(path: &str) -> bool {
    path.starts_with("test_") || path.contains("/test_")
}
