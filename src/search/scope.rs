//! Enclosing-scope annotator: given a `(file, line)`, return the nearest
//! enclosing definition, qualified with its containing type or module.
//! Used by the search formatter to annotate usages with their containing
//! function/class/module, and internally by `callers::find_enclosing_function`
//! when reporting the calling-function context of a call site.

use std::path::Path;

use crate::cache::OutlineCache;
use crate::lang::treesitter::extract_definition_name;

/// Type-like node kinds that can enclose a function definition.
const TYPE_KINDS: &[&str] = &[
    "class_declaration",
    "class_definition",
    "class",
    "class_specifier",
    "struct_item",
    "struct_specifier",
    "impl_item",
    "interface_declaration",
    "trait_item",
    "trait_declaration",
    "type_declaration",
    "enum_item",
    "enum_declaration",
    "module",
    "mod_item",
    "namespace_definition",
];

/// Resolved enclosing-definition context for a (file, line). Used by the
/// search formatter to annotate usages with their containing scope.
#[derive(Debug)]
pub struct EnclosingScope {
    /// Normalized kind label (e.g. `"function"`, `"class"`, `"struct"`).
    pub kind: &'static str,
    /// Identifier of the definition. Qualified with its enclosing type or
    /// module when one wraps it (e.g. `"Class.method"`, `"Module.func"`).
    pub name: String,
}

/// Walk up the AST from `node` to the nearest definition, qualified with its
/// enclosing type/module if one wraps it. Returns the AST node so the caller
/// can read its kind, plus the rendered name and line range.
pub(super) fn walk_to_enclosing_definition<'a>(
    node: tree_sitter::Node<'a>,
    lines: &[&str],
    lang: crate::types::Lang,
) -> Option<(tree_sitter::Node<'a>, String, (u32, u32))> {
    let definitions = &crate::lang::spec::spec(lang).definitions;
    let (definition, name) = std::iter::successors(Some(node), tree_sitter::Node::parent)
        .find_map(|candidate| {
            let name = (definitions.is_definition)(candidate, lines)
                .then(|| (definitions.extract_name)(candidate, lines))??;
            Some((candidate, name))
        })?;
    let range = (
        definition.start_position().row as u32 + 1,
        definition.end_position().row as u32 + 1,
    );

    let container = std::iter::successors(definition.parent(), tree_sitter::Node::parent).find_map(
        |container| {
            if TYPE_KINDS.contains(&container.kind()) {
                // Grammars without a `name` field (Kotlin) name types through the spec.
                extract_definition_name(container, lines)
                    .or_else(|| (definitions.extract_name)(container, lines))
            } else if (definitions.is_container)(container, lines) {
                (definitions.extract_name)(container, lines)
            } else {
                None
            }
        },
    );
    let name = match container {
        Some(container) => format!("{container}.{name}"),
        None => name,
    };
    Some((definition, name, range))
}

/// Find the nearest enclosing definition for `(path, line)` by re-parsing
/// the file with tree-sitter (cached on `OutlineCache`). AST-correct across
/// every language tilth supports — replaces parsing the rendered outline
/// string back into structured data.
///
/// Returns `None` if the file isn't a code file, the parse fails, or `line`
/// sits at the top level outside any definition.
pub fn enclosing_definition_at(
    path: &Path,
    line: u32,
    cache: &OutlineCache,
) -> Option<EnclosingScope> {
    if line == 0 {
        return None;
    }
    let parsed = cache.get_or_parse(path)?;
    let lines: Vec<&str> = parsed.content().lines().collect();
    let row = (line - 1) as usize;
    if row >= lines.len() {
        return None;
    }

    let point = tree_sitter::Point { row, column: 0 };
    let target = parsed
        .tree()
        .root_node()
        .descendant_for_point_range(point, point)?;

    let (def_node, name, _range) = walk_to_enclosing_definition(target, &lines, parsed.lang)?;
    Some(EnclosingScope {
        kind: kind_label(def_node, &lines, parsed.lang),
        name,
    })
}

/// For each of `lines`, the line span of the largest syntax node around it that
/// spans at most `max_lines` lines, below the file root: the complete statement,
/// block, or small definition a hit sits in. An entry is `None` when `source`
/// does not parse or no such node is larger than the line itself. The file
/// parses and splits into lines once for all of `lines`.
pub(crate) fn bounded_construct_spans(
    path: &Path,
    source: &str,
    lines: &[u32],
    max_lines: u32,
    cache: &OutlineCache,
) -> Vec<Option<(u32, u32)>> {
    let Some(parsed) = cache.parse_source(path, source) else {
        return vec![None; lines.len()];
    };
    let root = parsed.tree().root_node();
    let source_lines: Vec<&str> = source.lines().collect();
    lines
        .iter()
        .map(|&line| span_at(root, &source_lines, line, max_lines))
        .collect()
}

fn span_at(
    root: tree_sitter::Node,
    source_lines: &[&str],
    line: u32,
    max_lines: u32,
) -> Option<(u32, u32)> {
    let row = line.checked_sub(1)? as usize;
    let text = source_lines.get(row)?;
    // Anchor on the line's last character: the node there belongs to the
    // construct that continues past the line, not to a block the line closes.
    let trimmed = text.trim_end();
    let column = trimmed.len() - trimmed.chars().next_back()?.len_utf8();
    let point = tree_sitter::Point { row, column };
    let node = root.descendant_for_point_range(point, point)?;
    let mut best = None;
    for candidate in std::iter::successors(Some(node), tree_sitter::Node::parent) {
        if candidate.parent().is_none() {
            break;
        }
        let start = candidate.start_position().row as u32 + 1;
        let end_position = candidate.end_position();
        let mut end = end_position.row as u32 + 1;
        // A node that ends at column 0 owns only the newline of the row before.
        if end_position.column == 0 && end > start {
            end -= 1;
        }
        if end - start + 1 > max_lines {
            break;
        }
        best = Some((start, end));
    }
    best.filter(|&(start, end)| end > start)
}

/// Map a tree-sitter definition node to a short user-facing label. Every kind
/// we handle is enumerated here, so adding a new language grammar is "add the
/// node kind to this match" with no string heuristics elsewhere.
fn kind_label(node: tree_sitter::Node, lines: &[&str], lang: crate::types::Lang) -> &'static str {
    match node.kind() {
        "function_declaration"
        | "function_definition"
        | "function_item"
        | "method_definition"
        | "method_declaration"
        | "method"
        | "singleton_method"
        | "decorated_definition" => "function",
        "class_declaration" | "class_definition" | "class" | "class_specifier" => "class",
        "struct_item" | "struct_specifier" => "struct",
        "interface_declaration" => "interface",
        "trait_declaration" | "trait_item" => "trait",
        "type_alias_declaration" | "type_item" | "type_declaration" => "type",
        "enum_item" | "enum_declaration" => "enum",
        "lexical_declaration" | "variable_declaration" | "var_declaration" => "variable",
        "const_item" | "const_declaration" => "const",
        "static_item" => "static",
        "property_declaration" => "property",
        "mod_item" | "namespace_definition" => "module",
        "object_declaration" => "object",
        "impl_item" => "impl",
        "export_statement" => "export",
        _ => (crate::lang::spec::spec(lang).definitions.kind_label)(node, lines)
            .unwrap_or("definition"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn write(dir: &Path, name: &str, content: &str) -> PathBuf {
        let p = dir.join(name);
        fs::write(&p, content).unwrap();
        p
    }

    fn bounded_construct_span(
        path: &Path,
        source: &str,
        line: u32,
        max_lines: u32,
        cache: &OutlineCache,
    ) -> Option<(u32, u32)> {
        bounded_construct_spans(path, source, &[line], max_lines, cache)[0]
    }

    #[test]
    fn bounded_spans_resolve_every_line_from_one_parse() {
        let source = "def host():\n    call(\n        a,\n    )\n    other(\n        b,\n    )\n";
        let cache = OutlineCache::new();
        let spans = bounded_construct_spans(Path::new("a.py"), source, &[2, 5, 9], 3, &cache);
        assert_eq!(spans, vec![Some((2, 4)), Some((5, 7)), None]);
    }

    #[test]
    fn bounded_span_completes_a_multi_line_call() {
        let source = "def host():\n    call(\n        a,\n    )\n    other()\n";
        let cache = OutlineCache::new();
        let span = bounded_construct_span(Path::new("a.py"), source, 2, 12, &cache);
        assert_eq!(span, Some((1, 5)), "the whole small function fits");
        let span = bounded_construct_span(Path::new("a.py"), source, 2, 3, &cache);
        assert_eq!(
            span,
            Some((2, 4)),
            "the statement fits, its function does not"
        );
    }

    #[test]
    fn bounded_span_includes_the_enclosing_small_block() {
        let source = "if (!module.parent) {\n  app.listen(3000);\n  log('started');\n}\n";
        let cache = OutlineCache::new();
        let span = bounded_construct_span(Path::new("a.js"), source, 2, 12, &cache);
        assert_eq!(span, Some((1, 4)));
    }

    #[test]
    fn bounded_span_completes_a_call_that_continues_past_the_hit_line() {
        let cache = OutlineCache::new();
        let js = "function host(x) {\n  if (x) {\n    other();\n  } else if (marker_call(a,\n      b)) {\n    done();\n  }\n}\n";
        assert_eq!(
            bounded_construct_span(Path::new("a.js"), js, 4, 4, &cache),
            Some((4, 7)),
            "a leading `}}` must not anchor the span to the block before the hit"
        );
        let py = "def host():\n    x = prep(); marker_call(\n        a,\n    )\n    y = 1\n    z = 2\n    w = 3\n";
        assert_eq!(
            bounded_construct_span(Path::new("a.py"), py, 2, 4, &cache),
            Some((2, 4)),
            "a second statement on the hit line must be completed"
        );
    }

    #[test]
    fn bounded_span_is_none_for_a_single_line_or_an_oversized_construct() {
        let cache = OutlineCache::new();
        let one = "x = 1\ny = 2\n";
        assert_eq!(
            bounded_construct_span(Path::new("a.py"), one, 1, 12, &cache),
            None
        );
        let wide = format!("call(\n{}\n)\n", "    a,\n".repeat(30));
        assert_eq!(
            bounded_construct_span(Path::new("a.py"), &wide, 1, 12, &cache),
            None
        );
        assert_eq!(
            bounded_construct_span(Path::new("a.md"), "# a\n\ntext\n", 3, 12, &cache),
            None
        );
        assert_eq!(
            bounded_construct_span(Path::new("a.py"), one, 0, 12, &cache),
            None
        );
    }

    #[test]
    fn enclosing_at_rust_top_level_function() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "a.rs", "fn foo() {\n    let x = 1;\n}\n");
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 2, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "foo");
    }

    #[test]
    fn enclosing_at_rust_method_inside_mod() {
        // mod_item has a name field; impl_item does not, so the qualifier path
        // exercised here is mod-name → method-name.
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.rs",
            "mod outer {\n    fn helper() {\n        let x = 1;\n    }\n}\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 3, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "outer.helper");
    }

    #[test]
    fn enclosing_at_typescript_method_qualifies_with_class() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.ts",
            "class Foo {\n  bar() {\n    const x = 1;\n  }\n}\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 3, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "Foo.bar");
    }

    #[test]
    fn enclosing_at_python_method_qualifies_with_class() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.py",
            "class Foo:\n    def bar(self):\n        x = 1\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 3, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "Foo.bar");
    }

    #[test]
    fn enclosing_at_elixir_def_qualifies_with_module() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.ex",
            "defmodule Foo do\n  def bar do\n    :ok\n  end\nend\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 3, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "Foo.bar");
    }

    #[test]
    fn enclosing_at_nested_elixir_def_qualifies_with_outer_def() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.ex",
            "defmodule Foo do\n  def outer do\n    def inner do\n      :ok\n    end\n  end\nend\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 4, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "outer.inner");
    }

    #[test]
    fn enclosing_at_elixir_defmodule_kind_is_module() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.ex",
            "defmodule Foo do\n  @moduledoc \"hi\"\nend\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 2, &cache).unwrap();
        assert_eq!(scope.kind, "module");
        assert_eq!(scope.name, "Foo");
    }

    #[test]
    fn enclosing_at_top_level_returns_none() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "a.rs", "// just a comment\nfn foo() {}\n");
        let cache = OutlineCache::new();
        assert!(enclosing_definition_at(&p, 1, &cache).is_none());
    }

    #[test]
    fn enclosing_at_zero_line_returns_none() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "a.rs", "fn foo() {}\n");
        let cache = OutlineCache::new();
        assert!(enclosing_definition_at(&p, 0, &cache).is_none());
    }

    #[test]
    fn enclosing_at_non_code_returns_none() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "a.md", "# heading\n\nsome text\n");
        let cache = OutlineCache::new();
        assert!(enclosing_definition_at(&p, 3, &cache).is_none());
    }

    #[test]
    fn enclosing_at_caches_parse_across_calls() {
        // Two calls into the same file should reuse the cached parse —
        // observable indirectly by mutating the file between calls without
        // touching mtime: the first parse wins, the second sees stale data
        // because the mtime didn't change. (Test only asserts the cache hit
        // path returns the first-parse result.)
        let tmp = tempfile::tempdir().unwrap();
        let p = write(tmp.path(), "a.rs", "fn foo() { let x = 1; }\n");
        let cache = OutlineCache::new();
        let a = enclosing_definition_at(&p, 1, &cache).unwrap();
        let b = enclosing_definition_at(&p, 1, &cache).unwrap();
        assert_eq!(a.name, b.name);
        assert_eq!(a.kind, b.kind);
    }

    #[test]
    fn enclosing_at_kind_labels_for_common_definition_kinds() {
        // One case per kind_label match arm beyond `function`/`module`,
        // so a regression that miscategorizes (e.g.) a Rust `struct` as
        // `definition` would surface here.
        let cases: &[(&str, &str, u32, &str, &str)] = &[
            ("a.rs", "struct Foo { x: u32 }\n", 1, "struct", "Foo"),
            ("b.rs", "enum Color { Red, Blue }\n", 1, "enum", "Color"),
            (
                "c.rs",
                "trait Greeter { fn hi(&self); }\n",
                1,
                "trait",
                "Greeter",
            ),
            (
                "d.ts",
                "interface Shape { area(): number; }\n",
                1,
                "interface",
                "Shape",
            ),
            ("e.ts", "class Widget { x = 1; }\n", 1, "class", "Widget"),
        ];
        let cache = OutlineCache::new();
        for (filename, content, line, kind, name) in cases {
            let tmp = tempfile::tempdir().unwrap();
            let p = write(tmp.path(), filename, content);
            let scope = enclosing_definition_at(&p, *line, &cache)
                .unwrap_or_else(|| panic!("no scope returned for {filename}"));
            assert_eq!(scope.kind, *kind, "kind mismatch for {filename}");
            assert_eq!(scope.name, *name, "name mismatch for {filename}");
        }
    }

    #[test]
    fn enclosing_at_kotlin_method_qualifies_with_class() {
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.kt",
            "class Service {\n    fun handle() {\n        val x = 1\n    }\n}\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 3, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "Service.handle");
    }

    #[test]
    fn enclosing_at_rust_impl_block_does_not_qualify_with_type() {
        // tree-sitter-rust's `impl_item` exposes its type via a `type` field,
        // not via the `name`/`identifier`/`declarator` fields that
        // extract_definition_name probes. So methods inside `impl Foo {...}`
        // produce the bare function name, not `"Foo.bar"`. Pre-existing
        // behavior of find_enclosing_function — pinned here so a future
        // qualifier improvement is an intentional, visible change.
        let tmp = tempfile::tempdir().unwrap();
        let p = write(
            tmp.path(),
            "a.rs",
            "struct Foo;\nimpl Foo {\n    fn bar(&self) {\n        let x = 1;\n    }\n}\n",
        );
        let cache = OutlineCache::new();
        let scope = enclosing_definition_at(&p, 4, &cache).unwrap();
        assert_eq!(scope.kind, "function");
        assert_eq!(scope.name, "bar");
    }
}
