use crate::lang::treesitter::{extract_definition_name, node_text_simple, NodeTextMode};
use crate::types::{Lang, OutlineEntry, OutlineKind};

/// Get the tree-sitter Language for a given Lang variant.
pub fn outline_language(lang: Lang) -> Option<tree_sitter::Language> {
    crate::lang::spec::spec(lang).grammar.map(Into::into)
}

pub(crate) fn canonical_start_line(node: tree_sitter::Node, lang: Lang) -> u32 {
    (crate::lang::spec::spec(lang).canonical_anchor)(node)
        .start_position()
        .row as u32
        + 1
}

/// Parse markdown content into a tree-sitter block tree.
///
/// Returns `None` if the parser fails to set the language (should not happen
/// in practice). The block grammar is what tilth's outline / definition
/// scanners need: it emits `atx_heading`, `setext_heading`, `section`, and
/// `fenced_code_block` nodes. Inline structure (emphasis, links inside the
/// heading text) is parsed by a separate inline grammar tilth doesn't use —
/// heading text is read as the raw inline node's text.
///
/// Centralised so both `read::outline::markdown` and
/// `search::symbol::find_defs_markdown_buf` configure the parser the same
/// way.
pub fn parse_markdown(content: &str) -> Option<tree_sitter::Tree> {
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&tree_sitter_md::LANGUAGE.into()).ok()?;
    parser.parse(content, None)
}

/// Map an `atx_heading` or `setext_heading` node to its 1-6 level by
/// inspecting the marker child. Returns `None` for malformed nodes.
pub fn heading_level(node: tree_sitter::Node) -> Option<u8> {
    let kind = node.kind();
    if kind == "atx_heading" {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "atx_h1_marker" => return Some(1),
                "atx_h2_marker" => return Some(2),
                "atx_h3_marker" => return Some(3),
                "atx_h4_marker" => return Some(4),
                "atx_h5_marker" => return Some(5),
                "atx_h6_marker" => return Some(6),
                _ => {}
            }
        }
        None
    } else if kind == "setext_heading" {
        // setext H1: `=====`; H2: `-----`. Marker is a child node.
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "setext_h1_underline" => return Some(1),
                "setext_h2_underline" => return Some(2),
                _ => {}
            }
        }
        None
    } else {
        None
    }
}

/// Read the heading text of an `atx_heading` / `setext_heading` node from
/// pre-split source lines. Returns the inline content with surrounding
/// whitespace + trailing `#`s (for ATX-closed headings like `## Foo ##`)
/// trimmed, matching the previous hand-rolled scanner's output.
pub fn heading_text(node: tree_sitter::Node, lines: &[&str]) -> String {
    // Both heading kinds expose their inline content as an `inline` child.
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "inline" {
            let text = node_text_simple(child, lines, NodeTextMode::Full);
            return text.trim().trim_end_matches('#').trim().to_string();
        }
    }
    String::new()
}

/// Walk top-level children of the root node, extracting outline entries.
pub(crate) fn walk_top_level(
    root: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
) -> Vec<OutlineEntry> {
    let mut cursor = root.walk();
    collect_sibling_entries(root.children(&mut cursor), lines, lang, 0)
}

/// Convert a sibling sequence into entries while associating only contiguous,
/// language-approved leading adornments with the following declaration.
fn collect_sibling_entries<'tree>(
    children: impl Iterator<Item = tree_sitter::Node<'tree>>,
    lines: &[&str],
    lang: Lang,
    depth: usize,
) -> Vec<OutlineEntry> {
    let policy = crate::lang::spec::spec(lang).attach_leading_adornment;
    let mut entries = Vec::new();
    let mut pending = Vec::new();

    for child in children {
        if let Some(mut entry) = node_to_entry(child, lines, lang, depth) {
            let attach_pending = pending.last().is_some_and(|last| contiguous(*last, child))
                && pending
                    .iter()
                    .all(|adornment| policy(*adornment, Some(child), lines));
            if attach_pending {
                entry.span_start_line = pending[0].start_position().row as u32 + 1;
            }
            pending.clear();
            entries.push(entry);
        } else if policy(child, None, lines) {
            if pending
                .last()
                .is_some_and(|previous| !contiguous(*previous, child))
            {
                pending.clear();
            }
            pending.push(child);
        } else {
            pending.clear();
        }
    }

    entries
}

fn contiguous(previous: tree_sitter::Node, next: tree_sitter::Node) -> bool {
    previous.end_position().row + 1 == next.start_position().row
}

fn wrapper_span_start_line(
    wrapper: tree_sitter::Node,
    inner: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
) -> u32 {
    let policy = crate::lang::spec::spec(lang).attach_leading_adornment;
    let mut pending = Vec::new();
    let mut cursor = wrapper.walk();
    for child in wrapper.children(&mut cursor) {
        if child.id() == inner.id() {
            let attached = pending.last().is_some_and(|last| contiguous(*last, child))
                && pending
                    .iter()
                    .all(|adornment| policy(*adornment, Some(inner), lines));
            return if attached {
                pending[0].start_position().row as u32 + 1
            } else {
                inner.start_position().row as u32 + 1
            };
        }
        if policy(child, None, lines) {
            if pending
                .last()
                .is_some_and(|previous| !contiguous(*previous, child))
            {
                pending.clear();
            }
            pending.push(child);
        } else {
            pending.clear();
        }
    }
    inner.start_position().row as u32 + 1
}

/// Convert a tree-sitter node to an `OutlineEntry` based on its kind.
fn node_to_entry(
    node: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
    depth: usize,
) -> Option<OutlineEntry> {
    let kind_str = node.kind();
    let spec = crate::lang::spec::spec(lang);
    let canonical = (spec.canonical_anchor)(node);
    let span_start_line = (spec.semantic_start)(node, canonical, lines);
    if spec.definition_wrappers.contains(&kind_str) {
        let (mut entry, inner, unnamed_wrapper) =
            if let Some(inner) = node.child_by_field_name("definition") {
                (node_to_entry(inner, lines, lang, depth)?, inner, false)
            } else {
                // Some grammars, such as JavaScript, expose wrapped declarations as unnamed children.
                let mut cursor = node.walk();
                let mut found = None;
                for child in node.children(&mut cursor) {
                    if let Some(entry) = node_to_entry(child, lines, lang, depth) {
                        found = Some((entry, child, true));
                        break;
                    }
                }
                found?
            };
        entry.span_start_line = if unnamed_wrapper {
            entry
                .span_start_line
                .min(span_start_line.min(wrapper_span_start_line(node, inner, lines, lang)))
        } else {
            wrapper_span_start_line(node, inner, lines, lang)
        };
        entry.end_line = entry.end_line.max(node.end_position().row as u32 + 1);
        return Some(entry);
    }
    let start_line = canonical.start_position().row as u32 + 1;
    let end_line = node.end_position().row as u32 + 1;

    let (kind, name, signature) = match kind_str {
        // Functions
        "function_declaration"
        | "function_definition"
        | "function_item"
        | "method_definition"
        | "method_declaration"
        | "constructor_declaration"
        | "init_declaration"
        | "deinit_declaration"
        | "protocol_function_declaration" => {
            let name = find_child_text(node, "name", lines)
                .or_else(|| find_child_text(node, "identifier", lines))
                .or_else(|| first_identifier_text(node, lines))
                .or_else(|| extract_definition_name(node, lines))
                .unwrap_or_else(|| {
                    // Swift deinit has no name field — use the node kind as name
                    if kind_str == "deinit_declaration" {
                        "deinit".into()
                    } else {
                        "<anonymous>".into()
                    }
                });
            let sig = extract_signature(start_line, lines);
            (OutlineKind::Function, name, Some(sig))
        }

        // Classes & structs
        "class_declaration" | "class_definition" => {
            let name = find_child_text(node, "name", lines)
                .or_else(|| find_child_text(node, "identifier", lines))
                .unwrap_or_else(|| "<anonymous>".into());
            (OutlineKind::Class, name, None)
        }
        "struct_item" | "struct_declaration" => {
            let name = find_child_text(node, "name", lines).unwrap_or_else(|| "<anonymous>".into());
            (OutlineKind::Struct, name, None)
        }

        // Interfaces & traits
        "interface_declaration"
        | "type_alias_declaration"
        | "trait_item"
        | "trait_declaration"
        | "trait_definition"
        | "protocol_declaration" => {
            let name = find_child_text(node, "name", lines).unwrap_or_else(|| "<anonymous>".into());
            (OutlineKind::Interface, name, None)
        }
        "type_item" | "type_definition" | "typealias_declaration" => {
            let name = find_child_text(node, "name", lines).unwrap_or_else(|| "<anonymous>".into());
            (OutlineKind::TypeAlias, name, None)
        }

        // Enums
        "enum_item" | "enum_declaration" | "enum_definition" => {
            let name = find_child_text(node, "name", lines).unwrap_or_else(|| "<anonymous>".into());
            (OutlineKind::Enum, name, None)
        }

        // Impl blocks (Rust)
        "impl_item" => {
            let name = find_child_text(node, "type", lines).unwrap_or_else(|| "<impl>".into());
            (OutlineKind::Module, format!("impl {name}"), None)
        }

        // Objects (Scala companion objects, singletons; Kotlin object declarations)
        "object_declaration" | "object_definition" => {
            let name = find_child_text(node, "name", lines)
                .or_else(|| find_child_text(node, "identifier", lines))
                .unwrap_or_else(|| "<anonymous>".into());
            (OutlineKind::Module, name, None)
        }

        // Constants and variables
        "const_item" | "const_declaration" | "static_item" => {
            let name = find_child_text(node, "name", lines)
                .or_else(|| first_identifier_text(node, lines))
                .or_else(|| extract_definition_name(node, lines))
                .unwrap_or_else(|| "<const>".into());
            (OutlineKind::Constant, name, None)
        }
        "val_definition" => {
            let name = first_identifier_text(node, lines).unwrap_or_else(|| "<val>".into());
            (OutlineKind::ImmutableVariable, name, None)
        }
        "lexical_declaration" | "variable_declaration" | "var_definition" | "var_declaration" => {
            let name = first_identifier_text(node, lines)
                .or_else(|| extract_definition_name(node, lines))
                .unwrap_or_else(|| "<var>".into());
            (OutlineKind::Variable, name, None)
        }

        // Properties (C#, Swift, Kotlin)
        "property_declaration" | "protocol_property_declaration" => {
            let name = find_child_text(node, "name", lines)
                .or_else(|| first_identifier_text(node, lines))
                .unwrap_or_else(|| "<property>".into());
            let sig = extract_signature(start_line, lines);
            (OutlineKind::Property, name, Some(sig))
        }

        // Imports — collect as a group
        "import_statement"
        | "import_declaration"
        | "import"
        | "use_declaration"
        | "namespace_use_declaration"
        | "use_item"
        | "using_directive" => {
            let text = node_text(node, lines);
            (OutlineKind::Import, text, None)
        }

        // Exports — `export` is a modifier on a wrapped declaration, not a
        // peer of `function`/`class`/`const`. Recurse into the inner
        // declaration so the entry renders with its real kind. Falling back to
        // `OutlineKind::Export` only when there is no nameable declaration
        // inside (`export { … }`, `export * from …`, `export default <expr>`).
        // Without this, `export_statement`'s `name` is the full source span
        // (already starts with `export `), and the renderer prepends the
        // `Export` kind_label `"export"` again — producing the doubled-keyword
        // outline header `export export async function foo(`.
        "export_statement" => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if let Some(mut inner) = node_to_entry(child, lines, lang, depth) {
                    // `export` is an ownership adornment; keep the inner
                    // declaration's canonical anchor and extend its span.
                    inner.span_start_line = inner.span_start_line.min(span_start_line);
                    inner.end_line = inner.end_line.max(node.end_position().row as u32 + 1);
                    return Some(inner);
                }
            }
            // No nameable inner declaration; strip leading `export ` so the
            // rendered name doesn't duplicate the `kind_label`.
            let raw = node_text(node, lines);
            let name = raw
                .strip_prefix("export ")
                .map(str::to_string)
                .unwrap_or(raw);
            (OutlineKind::Export, name, None)
        }

        // Module declarations
        "mod_item"
        | "module"
        | "internal_module"
        | "namespace_declaration"
        | "namespace_definition"
        | "file_scoped_namespace_declaration" => {
            let name = find_child_text(node, "name", lines).unwrap_or_else(|| "<module>".into());
            (OutlineKind::Module, name, None)
        }

        // Elixir: all definitions are `call` nodes distinguished by target identifier
        "call" if lang == Lang::Elixir => {
            return elixir_call_to_entry(node, lines, lang, depth);
        }

        // Elixir: @type, @typep, @opaque are unary_operator nodes
        "unary_operator" if lang == Lang::Elixir => {
            return elixir_attr_to_entry(node, lines);
        }

        // Bash: top-level variable assignments (`MY_VAR=value`, `ARR[0]=value`)
        "variable_assignment" if lang == Lang::Bash => {
            let name = assignment_name(node, lines).unwrap_or_else(|| "<var>".into());
            (OutlineKind::Variable, name, None)
        }

        // Bash: top-level `export` / `declare` / `readonly` declarations. The name
        // is the `name` of the inner variable_assignment (`export FOO=bar`) or a
        // bare variable_name child (`export FOO`). Function-local `local`
        // declarations are nested in function bodies, so walk_top_level never
        // reaches them here. Multi-variable declarations surface their first name.
        "declaration_command" if lang == Lang::Bash => {
            let mut cursor = node.walk();
            let name = node
                .children(&mut cursor)
                .find_map(|child| match child.kind() {
                    "variable_assignment" => assignment_name(child, lines),
                    "variable_name" => Some(node_text(child, lines)),
                    _ => None,
                })?;
            (OutlineKind::Variable, name, None)
        }

        _ => return None,
    };

    // Collect children for classes, impls, modules, traits/interfaces
    let is_namespace = matches!(
        kind_str,
        "internal_module"
            | "namespace_declaration"
            | "namespace_definition"
            | "file_scoped_namespace_declaration"
    );
    let children = if matches!(
        kind,
        OutlineKind::Class | OutlineKind::Struct | OutlineKind::Module | OutlineKind::Interface
    ) && depth < 1
    {
        // Namespaces are transparent wrappers — don't consume a depth level,
        // so classes inside namespaces still collect their methods.
        let child_depth = if is_namespace { depth } else { depth + 1 };
        collect_children(node, lines, lang, child_depth)
    } else {
        Vec::new()
    };

    // Extract doc comment if present
    let doc = extract_doc(node, lines);

    Some(OutlineEntry {
        kind,
        name,
        start_line,
        span_start_line,
        end_line,
        signature,
        children,
        doc,
    })
}

fn is_transparent_declaration_wrapper(node: tree_sitter::Node, lang: Lang) -> bool {
    crate::lang::spec::spec(lang)
        .definition_wrappers
        .contains(&node.kind())
}

/// Canonical outline name for a single container node, with no child recursion.
/// Used by grok's AST owner finder so the owner string stays in lockstep with how
/// the outline tree names impls/modules/classes (e.g. `"impl Foo"`). `None` for
/// non-container or unnamed nodes. Passing `depth = 1` skips child collection.
pub(crate) fn container_entry_name(
    node: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
) -> Option<String> {
    if is_transparent_declaration_wrapper(node, lang) {
        return None;
    }
    node_to_entry(node, lines, lang, 1)
        .filter(|entry| {
            matches!(
                entry.kind,
                OutlineKind::Class
                    | OutlineKind::Struct
                    | OutlineKind::Interface
                    | OutlineKind::Module
                    | OutlineKind::Enum
            )
        })
        .map(|entry| entry.name)
}

/// Collect child entries from a class/struct/impl body.
fn collect_children(
    node: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
    depth: usize,
) -> Vec<OutlineEntry> {
    let mut cursor = node.walk();

    // Look for a body node first (C# uses `declaration_list` instead of
    // `*_body`/`*_block`).
    let body = node.children(&mut cursor).find(|c| {
        let k = c.kind();
        k.contains("body") || k.contains("block") || k == "declaration_list"
    });

    let parent = body.unwrap_or(node);
    let mut cursor = parent.walk();
    collect_sibling_entries(parent.children(&mut cursor), lines, lang, depth)
}

/// Extract the canonical declaration line as a signature.
fn extract_signature(start_line: u32, lines: &[&str]) -> String {
    let start_row = start_line.saturating_sub(1) as usize;
    if start_row < lines.len() {
        let line = lines[start_row].trim();
        // Truncate at opening brace
        if let Some(pos) = line.find('{') {
            return line[..pos].trim().to_string();
        }
        if line.ends_with(':') {
            // Python — truncate at trailing colon (for `def foo(x: int):` etc.)
            if let Some(pos) = line.rfind(':') {
                return line[..pos].trim().to_string();
            }
        }
        // Elixir — truncate at ` do` (block form) or `, do:` (keyword form).
        // Safe for other languages: C/Java/Go/Rust hit the `{` branch above,
        // Python hits the `:` branch. Only Elixir uses ` do` as a block delimiter.
        if let Some(pos) = line.rfind(" do") {
            let after = &line[pos + 3..];
            if after.is_empty() || after.starts_with('\n') {
                return line[..pos].trim().to_string();
            }
        }
        if let Some(pos) = line.find(", do:") {
            return line[..pos].trim().to_string();
        }
        // Full first line, truncated
        if line.len() > 120 {
            format!("{}...", crate::types::truncate_str(line, 117))
        } else {
            line.to_string()
        }
    } else {
        String::new()
    }
}

/// Find a named child and return its text.
fn find_child_text(node: tree_sitter::Node, field: &str, lines: &[&str]) -> Option<String> {
    node.child_by_field_name(field).map(|n| node_text(n, lines))
}

/// Resolve the variable name from an assignment `name` field, unwrapping a
/// `subscript` (`ARR[0]=x`) to its base `variable_name` so the symbol
/// surfaces as `ARR`, not `ARR[0]`.
fn assignment_name(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let name = node.child_by_field_name("name")?;
    if name.kind() == "subscript" {
        let mut cursor = name.walk();
        let base = name
            .children(&mut cursor)
            .find(|c| c.kind() == "variable_name")
            .unwrap_or(name);
        Some(node_text(base, lines))
    } else {
        Some(node_text(name, lines))
    }
}

/// Get the text of a node, truncated to the first line.
fn node_text(node: tree_sitter::Node, lines: &[&str]) -> String {
    node_text_simple(node, lines, NodeTextMode::Truncated)
}

/// Find the first identifier-like child.
/// Recurses one level through declarators and `variable_declaration` nodes to find
/// the actual identifier inside wrapper nodes (e.g. Kotlin `property_declaration`
/// → `variable_declaration` → `simple_identifier`).
fn first_identifier_text(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        let kind = child.kind();
        if kind.contains("identifier") || kind.contains("name") {
            let text = node_text(child, lines);
            if !text.is_empty() {
                return Some(text);
            }
        }
        // Recurse one level through wrapper nodes (variable_declarator, variable_declaration)
        if kind.contains("declarator") || kind.contains("declaration") {
            let mut inner = child.walk();
            for grandchild in child.children(&mut inner) {
                if grandchild.kind().contains("identifier") {
                    let text = node_text(grandchild, lines);
                    if !text.is_empty() {
                        return Some(text);
                    }
                }
            }
        }
    }
    None
}

/// Extract a doc comment from the previous sibling.
fn extract_doc(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let prev = node.prev_sibling()?;
    let kind = prev.kind();
    if kind.contains("comment") || kind.contains("doc") {
        let text = node_text(prev, lines);
        let trimmed = text
            .trim_start_matches("///")
            .trim_start_matches("//!")
            .trim_start_matches("/**")
            .trim_start_matches('#')
            .trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_string())
        }
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Elixir-specific outline helpers
// ---------------------------------------------------------------------------

/// Elixir function-like definition keywords that produce `OutlineKind::Function`.
/// This is the subset of definition keywords handled uniformly (extract function
/// name from arguments). Container keywords (`defmodule`, `defprotocol`, `defimpl`,
/// `defstruct`, `defexception`) have their own match arms in `elixir_call_to_entry`.
/// See also `ELIXIR_DEFINITION_TARGETS` in `elixir.rs` for the complete set.
const ELIXIR_DEF_KEYWORDS: &[&str] = &[
    "def",
    "defp",
    "defmacro",
    "defmacrop",
    "defguard",
    "defguardp",
    "defdelegate",
];

/// Convert an Elixir `call` node to an outline entry.
///
/// In the Elixir tree-sitter grammar, `defmodule`, `def`, `defp`, `defstruct`,
/// etc. are all `call` nodes whose `target` field is an identifier like `"def"`.
fn elixir_call_to_entry(
    node: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
    depth: usize,
) -> Option<OutlineEntry> {
    let target = node.child_by_field_name("target")?;
    let keyword = node_text(target, lines);
    let start_line = node.start_position().row as u32 + 1;
    let end_line = node.end_position().row as u32 + 1;

    let (kind, name, signature) = match keyword.as_str() {
        "defmodule" => {
            let name = elixir_first_arg_text(node, lines)?;
            (OutlineKind::Module, name, None)
        }
        kw if ELIXIR_DEF_KEYWORDS.contains(&kw) => {
            let name = elixir_func_name(node, lines)?;
            let sig = extract_signature(start_line, lines);
            (OutlineKind::Function, name, Some(sig))
        }
        "defstruct" | "defexception" => (OutlineKind::Struct, keyword.clone(), None),
        "defprotocol" => {
            let name = elixir_first_arg_text(node, lines)?;
            (OutlineKind::Interface, name, None)
        }
        "defimpl" => {
            let name = elixir_first_arg_text(node, lines)?;
            (OutlineKind::Module, format!("impl {name}"), None)
        }
        "use" | "import" | "alias" | "require" => {
            let text = node_text(node, lines);
            (OutlineKind::Import, text, None)
        }
        _ => return None,
    };

    // Collect children for modules, protocols, impls
    let children = if matches!(kind, OutlineKind::Module | OutlineKind::Interface) && depth < 1 {
        elixir_collect_children(node, lines, lang, depth + 1)
    } else {
        Vec::new()
    };

    // Extract @doc / @moduledoc from previous sibling
    let doc = elixir_extract_doc(node, lines);

    Some(OutlineEntry {
        kind,
        name,
        start_line,
        span_start_line: start_line,
        end_line,
        signature,
        children,
        doc,
    })
}

/// Convert an Elixir `unary_operator` node (`@type`, `@typep`, `@opaque`) to an outline entry.
fn elixir_attr_to_entry(node: tree_sitter::Node, lines: &[&str]) -> Option<OutlineEntry> {
    let operand = node.child_by_field_name("operand")?;
    if operand.kind() != "call" {
        return None;
    }
    let target = operand.child_by_field_name("target")?;
    let attr_name = node_text(target, lines);
    let start_line = node.start_position().row as u32 + 1;
    let end_line = node.end_position().row as u32 + 1;
    match attr_name.as_str() {
        "type" | "typep" | "opaque" => {
            let name = elixir_type_name(operand, lines)?;
            let sig = node_text(node, lines);
            Some(OutlineEntry {
                kind: OutlineKind::TypeAlias,
                name,
                start_line,
                span_start_line: start_line,
                end_line,
                signature: Some(sig),
                children: Vec::new(),
                doc: None,
            })
        }
        "callback" | "macrocallback" => {
            let name = elixir_callback_name(operand, lines)?;
            let sig = node_text(node, lines);
            Some(OutlineEntry {
                kind: OutlineKind::Function,
                name,
                start_line,
                span_start_line: start_line,
                end_line,
                signature: Some(sig),
                children: Vec::new(),
                doc: None,
            })
        }
        _ => None,
    }
}

/// Extract the first argument text from an Elixir call node.
/// For `defmodule Foo.Bar do ... end`, returns `"Foo.Bar"`.
fn elixir_first_arg_text(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let args = super::treesitter::elixir_arguments(node)?;
    let mut cursor = args.walk();
    for child in args.children(&mut cursor) {
        if child.is_named() {
            return Some(node_text(child, lines));
        }
    }
    None
}

/// Extract function name from an Elixir `def`/`defp` call node.
///
/// For `def greet(name) do ... end`, the AST is:
///   call[target=def] → arguments → call[target=greet] → arguments → ...
/// For `def greet(name), do: ...` (keyword form), same structure.
fn elixir_func_name(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let args = super::treesitter::elixir_arguments(node)?;
    let mut cursor = args.walk();
    for child in args.children(&mut cursor) {
        if !child.is_named() {
            continue;
        }
        return super::treesitter::elixir_extract_func_head_name(child, lines);
    }
    None
}

/// Extract type name from an Elixir `@type` call.
/// For `@type t :: %{...}`, the call operand is `type t :: %{...}`,
/// and we extract `t` from the first argument.
fn elixir_type_name(call: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let args = super::treesitter::elixir_arguments(call)?;
    let mut cursor = args.walk();
    for child in args.children(&mut cursor) {
        if !child.is_named() {
            continue;
        }
        // `type t :: ...` → binary_operator with left=identifier
        if child.kind() == "binary_operator" {
            if let Some(left) = child.child_by_field_name("left") {
                // left may be a call like `t()` or an identifier `t`
                if left.kind() == "call" {
                    if let Some(target) = left.child_by_field_name("target") {
                        return Some(node_text(target, lines));
                    }
                }
                return Some(node_text(left, lines));
            }
        }
        // Bare identifier
        if child.kind() == "identifier" {
            return Some(node_text(child, lines));
        }
    }
    None
}

/// Extract callback name from an Elixir `@callback` call.
/// For `@callback handle_event(event :: term()) :: :ok`, the call operand is
/// `callback handle_event(...) :: :ok`. The arguments contain a `binary_operator`
/// with `::`, whose left side is a `call` with target = the callback name.
fn elixir_callback_name(call: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let args = super::treesitter::elixir_arguments(call)?;
    let mut cursor = args.walk();
    for child in args.children(&mut cursor) {
        if !child.is_named() {
            continue;
        }
        if child.kind() == "binary_operator" {
            // `handle_event(...) :: return_type` → left is the function head
            if let Some(left) = child.child_by_field_name("left") {
                return super::treesitter::elixir_extract_func_head_name(left, lines);
            }
        }
        // Bare callback without return type spec (unlikely but handle it)
        return super::treesitter::elixir_extract_func_head_name(child, lines);
    }
    None
}

/// Collect child entries from an Elixir module/protocol/impl `do_block`.
///
/// This intentionally includes `use`/`alias`/`import`/`require` as import entries
/// inside module outlines. In Elixir these are structural — `use GenServer` injects
/// callbacks, `alias Foo.Bar` affects name resolution — so they provide useful
/// context alongside function definitions.
fn elixir_collect_children(
    node: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
    depth: usize,
) -> Vec<OutlineEntry> {
    let mut cursor = node.walk();

    // Find the do_block child.
    let Some(do_block) = node.children(&mut cursor).find(|c| c.kind() == "do_block") else {
        return Vec::new();
    };

    let mut cursor = do_block.walk();
    collect_sibling_entries(do_block.children(&mut cursor), lines, lang, depth)
}

/// Extract @doc or @moduledoc text from the previous sibling of an Elixir definition.
///
/// In Elixir, `@doc "text"` is a `unary_operator` node. We check if the
/// previous sibling is such a node and extract the string content.
fn elixir_extract_doc(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let prev = node.prev_sibling()?;
    if prev.kind() != "unary_operator" {
        return None;
    }
    let operand = prev.child_by_field_name("operand")?;
    if operand.kind() != "call" {
        return None;
    }
    let target = operand.child_by_field_name("target")?;
    let attr = node_text(target, lines);
    if attr != "doc" && attr != "moduledoc" {
        return None;
    }
    // Get the doc argument — use tree-sitter node types to handle all forms:
    //   `@doc "text"`           → string node
    //   `@doc """heredoc"""`    → string node (multi-line)
    //   `@doc ~S"""sigil"""`    → sigil node
    //   `@doc ~s"""sigil"""`    → sigil node
    //   `@doc false`            → boolean node (suppress docs)
    let args = super::treesitter::elixir_arguments(operand)?;
    let mut cursor = args.walk();
    for child in args.children(&mut cursor) {
        if !child.is_named() {
            continue;
        }
        match child.kind() {
            // `@doc false` suppresses documentation
            "boolean" => return None,
            // Regular string (`"text"`, `"""heredoc"""`) or sigil (`~S"""..."""`, `~s"""..."""`)
            "string" | "sigil" => {
                return elixir_extract_doc_string(child, lines);
            }
            _ => {}
        }
    }
    None
}

/// Extract the first meaningful line from an Elixir doc string or sigil node.
///
/// For single-line strings (`"text"`), returns the content without quotes.
/// For heredocs/sigils (`"""..."""`, `~S"""..."""`), returns the first
/// non-empty content line. Uses tree-sitter source lines rather than
/// fragile string trimming.
fn elixir_extract_doc_string(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let start_row = node.start_position().row;
    let end_row = node.end_position().row;

    if start_row == end_row {
        // Single-line: `"text"` or `~s"text"` — strip delimiters and sigil prefix
        let mut text = node_text(node, lines);
        // Strip sigil prefix (~s, ~S, etc.) if present
        if text.starts_with('~') && text.len() >= 2 {
            text = text[2..].to_string();
        }
        let trimmed = text.trim_matches('"').trim();
        if trimmed.is_empty() {
            return None;
        }
        return Some(trimmed.to_string());
    }

    // Multi-line (heredoc or sigil): scan interior lines for first non-empty content
    for row in (start_row + 1)..end_row {
        if row >= lines.len() {
            break;
        }
        let line = lines[row].trim();
        if !line.is_empty() && line != "\"\"\"" {
            return Some(line.to_string());
        }
    }
    None
}

/// Extract the source module name from an import statement text.
/// Handles: `use std::fs;` → `std::fs`, `import X from "react"` → `react`,
/// `from collections import X` → `collections`
///
/// The `lang` parameter is needed to disambiguate `use` (Rust path vs Elixir module)
/// and `import` (JS/TS `from` syntax vs Elixir/Python/Go bare module name).
pub(crate) fn extract_import_source(text: &str, lang: Option<crate::types::Lang>) -> String {
    let trimmed = text.trim().trim_end_matches(';');

    // Bash: `source ./lib.sh`, `. ./lib.sh`, or tab-separated variants
    if lang == Some(crate::types::Lang::Bash) {
        let after = trimmed
            .strip_prefix("source")
            .or_else(|| trimmed.strip_prefix('.'))
            .filter(|rest| rest.starts_with(char::is_whitespace))
            .map_or(trimmed, str::trim_start);
        // Skip variable-expanded paths (contain `$`)
        if after.contains('$') {
            return String::new();
        }
        return after.trim_matches(|c| c == '"' || c == '\'').to_string();
    }

    // Elixir: `use GenServer`, `import Kernel`, `alias Foo.Bar`, `require Logger`
    // Must be checked before the Rust `use` and JS `import` branches.
    if lang == Some(crate::types::Lang::Elixir) {
        for prefix in &["use ", "import ", "alias ", "require "] {
            if let Some(rest) = trimmed.strip_prefix(prefix) {
                return rest.split(',').next().unwrap_or(rest).trim().to_string();
            }
        }
        return trimmed.to_string();
    }

    // Rust: `use foo::bar` → `foo::bar`
    if let Some(rest) = trimmed.strip_prefix("use ") {
        return rest
            .split('{')
            .next()
            .unwrap_or(rest)
            .trim()
            .trim_end_matches("::")
            .to_string();
    }

    // JS/TS: `import ... from "source"` or `import "source"`
    if trimmed.starts_with("import") {
        if let Some(from_pos) = trimmed.find("from ") {
            let source = &trimmed[from_pos + 5..];
            return source
                .trim()
                .trim_matches(|c| c == '"' || c == '\'' || c == ';')
                .to_string();
        }
        // Direct import: `import "source"`
        let after = trimmed.strip_prefix("import ").unwrap_or("");
        return after
            .trim()
            .trim_matches(|c| c == '"' || c == '\'' || c == ';')
            .to_string();
    }

    // Python: `from module import ...` or `import module`
    if let Some(rest) = trimmed.strip_prefix("from ") {
        return rest.split_whitespace().next().unwrap_or("").to_string();
    }
    if let Some(rest) = trimmed.strip_prefix("import ") {
        return rest.split_whitespace().next().unwrap_or("").to_string();
    }

    // C/C++: #include "file.h" or #include <header>
    if let Some(rest) = trimmed.strip_prefix("#include") {
        return rest.trim().to_string(); // preserves quotes/angles for external detection
    }

    // Go: `import "source"` — already handled above via "import"
    // Fallback: first meaningful token
    trimmed
        .split_whitespace()
        .last()
        .unwrap_or(trimmed)
        .to_string()
}

fn parse_outline(content: &str, lang: Lang) -> Option<(tree_sitter::Tree, Vec<&str>)> {
    let ts_lang = outline_language(lang)?;
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&ts_lang).ok()?;
    let tree = parser.parse(content, None)?;
    Some((tree, content.lines().collect()))
}

pub(crate) fn is_path_line_entry_kind(kind: OutlineKind) -> bool {
    matches!(
        kind,
        OutlineKind::Function
            | OutlineKind::Class
            | OutlineKind::Struct
            | OutlineKind::Interface
            | OutlineKind::TypeAlias
            | OutlineKind::Enum
            | OutlineKind::Module
            | OutlineKind::TestSuite
            | OutlineKind::TestCase
    )
}

/// Return whether `entry` is a callable, type, or container path-line target.
///
/// Path-line and edit-block anchors intentionally ignore imports, exports,
/// variables, and other non-block outline entries.
fn owns_path_line(entry: &OutlineEntry, line: u32) -> bool {
    is_path_line_entry_kind(entry.kind) && (entry.span_start_line..=entry.end_line).contains(&line)
}

/// Get structured outline entries for file content.
pub fn get_outline_entries(content: &str, lang: Lang) -> Vec<OutlineEntry> {
    let Some((tree, lines)) = parse_outline(content, lang) else {
        return Vec::new();
    };
    walk_top_level(tree.root_node(), &lines, lang)
}

/// Parse once and return every callable, type, or container entry.
///
/// Entries are deepest-first and flattened. Transparent wrappers replace their
/// inner declaration entry structurally, so distinct declarations stay distinct.
pub(crate) fn get_deep_outline_entries(content: &str, lang: Lang) -> Vec<OutlineEntry> {
    let Some((tree, lines)) = parse_outline(content, lang) else {
        return Vec::new();
    };
    deep_outline_entries(tree.root_node(), &lines, lang)
}

/// Parse the full declaration tree for consumers that need deep parent/sibling context.
/// The regular outline remains shallow for display stability.
pub(crate) fn get_deep_outline_tree(content: &str, lang: Lang) -> Vec<OutlineEntry> {
    let Some((tree, lines)) = parse_outline(content, lang) else {
        return Vec::new();
    };
    let mut located = Vec::new();
    collect_located_entries(tree.root_node(), &lines, lang, &mut located);
    let mut flat = deep_outline_entries(tree.root_node(), &lines, lang);
    reconcile_semantic_spans(&mut located, &mut flat);
    build_located_tree(located)
}

struct LocatedEntry {
    start_byte: usize,
    end_byte: usize,
    entry: OutlineEntry,
    children: Vec<OutlineEntry>,
}

fn collect_located_entries(
    root: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
    entries: &mut Vec<LocatedEntry>,
) {
    let mut pending = vec![root];
    while let Some(node) = pending.pop() {
        if is_transparent_declaration_wrapper(node, lang) {
            let mut cursor = node.walk();
            let children: Vec<_> = node.children(&mut cursor).collect();
            pending.extend(children.into_iter().rev());
            continue;
        }
        if let Some(mut entry) = node_to_entry(node, lines, lang, 1) {
            if is_path_line_entry_kind(entry.kind) {
                entry.children.clear();
                entries.push(LocatedEntry {
                    start_byte: node.start_byte(),
                    end_byte: node.end_byte(),
                    entry,
                    children: Vec::new(),
                });
            }
        }
        let mut cursor = node.walk();
        let children: Vec<_> = node.children(&mut cursor).collect();
        pending.extend(children.into_iter().rev());
    }
}

fn reconcile_semantic_spans(entries: &mut [LocatedEntry], flat: &mut Vec<OutlineEntry>) {
    for located in entries {
        if let Some(index) = flat.iter().position(|candidate| {
            candidate.kind == located.entry.kind
                && candidate.name == located.entry.name
                && candidate.start_line == located.entry.start_line
                && candidate.end_line == located.entry.end_line
        }) {
            let candidate = flat.remove(index);
            located.entry.start_line = candidate.start_line;
            located.entry.span_start_line = candidate.span_start_line;
            located.entry.end_line = candidate.end_line;
            located.entry.signature = candidate.signature;
            located.entry.doc = candidate.doc;
        }
    }
}

fn build_located_tree(entries: Vec<LocatedEntry>) -> Vec<OutlineEntry> {
    let mut roots = Vec::new();
    let mut stack: Vec<LocatedEntry> = Vec::new();
    for located in entries {
        while stack.last().is_some_and(|parent| {
            located.start_byte < parent.start_byte || located.end_byte > parent.end_byte
        }) {
            finish_located(&mut stack, &mut roots);
        }
        stack.push(located);
    }
    while !stack.is_empty() {
        finish_located(&mut stack, &mut roots);
    }
    roots
}

fn finish_located(stack: &mut Vec<LocatedEntry>, roots: &mut Vec<OutlineEntry>) {
    let mut located = stack.pop().expect("stack is non-empty");
    located.entry.children = located.children;
    if let Some(parent) = stack.last_mut() {
        parent.children.push(located.entry);
    } else {
        roots.push(located.entry);
    }
}

pub(crate) fn deep_outline_entries(
    root: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
) -> Vec<OutlineEntry> {
    let mut entries = Vec::new();
    collect_deep_outline_entries(root, lines, lang, &mut entries);
    entries
}

fn collect_deep_outline_entries(
    root: tree_sitter::Node,
    lines: &[&str],
    lang: Lang,
    entries: &mut Vec<OutlineEntry>,
) {
    let mut pending = vec![(root, false)];
    while let Some((node, expanded)) = pending.pop() {
        let mut cursor = node.walk();
        let children: Vec<_> = node.children(&mut cursor).collect();
        if expanded {
            for mut entry in collect_sibling_entries(children.into_iter(), lines, lang, 1) {
                if is_path_line_entry_kind(entry.kind) {
                    entry.children.clear();
                    entries.push(entry);
                }
            }
            continue;
        }

        pending.push((node, true));
        let mut traversal_children = Vec::new();
        for child in children {
            if is_transparent_declaration_wrapper(child, lang) {
                let mut wrapper_cursor = child.walk();
                traversal_children.extend(wrapper_cursor.node().children(&mut wrapper_cursor));
            } else {
                traversal_children.push(child);
            }
        }
        pending.extend(
            traversal_children
                .into_iter()
                .rev()
                .map(|child| (child, false)),
        );
    }
}

fn get_outline_entries_and_entry(
    content: &str,
    lang: Lang,
    line: u32,
    accepts: fn(&OutlineEntry, u32) -> bool,
) -> (Vec<OutlineEntry>, Option<OutlineEntry>) {
    let Some((tree, lines)) = parse_outline(content, lang) else {
        return (Vec::new(), None);
    };
    let root = tree.root_node();
    let entry = deep_outline_entries(root, &lines, lang)
        .into_iter()
        .find(|entry| accepts(entry, line));
    (walk_top_level(root, &lines, lang), entry)
}

pub(crate) fn get_outline_entries_and_entry_at_line(
    content: &str,
    lang: Lang,
    line: u32,
) -> (Vec<OutlineEntry>, Option<OutlineEntry>) {
    get_outline_entries_and_entry(content, lang, line, owns_path_line)
}

/// Parse once, return the shallow outline, and resolve the deepest converted
/// declaration with the requested name and canonical start line. When provided,
/// `semantic_end` distinguishes declarations that share both values. If no
/// name-matching entry exists, retain the deepest exact identity so callers can
/// perform explicit moved-name recovery.
pub(crate) fn get_outline_entries_and_entry_by_name_at_start_line(
    content: &str,
    lang: Lang,
    name: &str,
    line: u32,
    semantic_end: Option<u32>,
) -> (Vec<OutlineEntry>, Option<OutlineEntry>) {
    let Some((tree, lines)) = parse_outline(content, lang) else {
        return (Vec::new(), None);
    };
    let root = tree.root_node();
    let mut deep_entries = deep_outline_entries(root, &lines, lang);
    let matches_identity = |entry: &OutlineEntry| {
        entry.start_line == line && semantic_end.is_none_or(|end| entry.end_line == end)
    };
    let entry_index = deep_entries
        .iter()
        .position(|entry| entry.name == name && matches_identity(entry))
        .or_else(|| deep_entries.iter().position(matches_identity));
    let entry = entry_index.map(|index| deep_entries.swap_remove(index));
    (walk_top_level(root, &lines, lang), entry)
}

/// Resolve one raw tree-sitter definition match against a converted entry from
/// the same parsed tree. Normal definitions use name/range identity; synthetic
/// implementation matches use their explicit trait/interface target.
pub(crate) fn find_entry_for_definition<'a>(
    entries: &'a [OutlineEntry],
    name: Option<&str>,
    line: u32,
    raw_range: (u32, u32),
    impl_target: Option<&str>,
) -> Option<&'a OutlineEntry> {
    entries.iter().find(|entry| {
        entry.end_line == raw_range.1
            && converted_definition_entry_matches(entry, name, line, impl_target)
    })
}

fn converted_definition_entry_matches(
    entry: &OutlineEntry,
    name: Option<&str>,
    line: u32,
    impl_target: Option<&str>,
) -> bool {
    if !(entry.span_start_line..=entry.end_line).contains(&line) {
        return false;
    }
    if impl_target.is_some() {
        return name
            .and_then(|name| name.split_once(" implements ").map(|(class, _)| class))
            .is_none_or(|class| entry.name == class);
    }
    name.is_some_and(|name| entry.name == name)
}

/// First outline entry named `name` (depth-first pre-order), returning its
/// 1-based inclusive semantic ownership span. This is the single canonical
/// symbol-walk shared by the `#symbol` read selector and block-anchor
/// resolution.
pub fn find_entry_by_name(entries: &[OutlineEntry], name: &str) -> Option<(u32, u32)> {
    for e in entries {
        if e.name == name {
            return Some((e.span_start_line, e.end_line));
        }
        if let Some(hit) = find_entry_by_name(&e.children, name) {
            return Some(hit);
        }
    }
    None
}

/// First outline entry whose canonical display/search anchor is `start_line`,
/// returning the full entry so consumers can choose its semantic span.
pub fn find_entry_by_start_line(
    entries: &[OutlineEntry],
    start_line: u32,
) -> Option<&OutlineEntry> {
    for entry in entries {
        if entry.start_line == start_line {
            return Some(entry);
        }
        if let Some(found) = find_entry_by_start_line(&entry.children, start_line) {
            return Some(found);
        }
    }
    None
}

#[cfg(test)]
mod markdown_helper_tests {
    use super::{heading_level, heading_text, parse_markdown};

    /// Walk the tree and collect every `atx_heading`/`setext_heading` node.
    fn collect_headings(tree: &tree_sitter::Tree) -> Vec<tree_sitter::Node<'_>> {
        let mut out = Vec::new();
        let mut cursor = tree.walk();
        let mut stack = vec![tree.root_node()];
        while let Some(node) = stack.pop() {
            if matches!(node.kind(), "atx_heading" | "setext_heading") {
                out.push(node);
            }
            for child in node.children(&mut cursor) {
                stack.push(child);
            }
        }
        out.sort_by_key(|n| n.start_position().row);
        out
    }

    #[test]
    fn parse_returns_block_tree_with_sections() {
        let src = "# Top\n\ncontent\n\n## Sub\n\nmore\n";
        let tree = parse_markdown(src).unwrap();
        let root = tree.root_node();
        assert_eq!(root.kind(), "document");
        // The document contains at least one section node.
        let mut cursor = root.walk();
        let has_section = root.children(&mut cursor).any(|c| c.kind() == "section");
        assert!(has_section, "expected document to contain section children");
    }

    #[test]
    fn fenced_code_blocks_do_not_emit_headings() {
        // The whole point: a `# foo` inside a fenced code block must NOT be
        // parsed as an atx_heading. The hand-rolled scanners had to track
        // fence state manually; the AST does this for free.
        let src = "# Real\n\n```python\n# fake heading\nprint('x')\n```\n\n## Also Real\n";
        let tree = parse_markdown(src).unwrap();
        let headings = collect_headings(&tree);
        let lines: Vec<&str> = src.lines().collect();
        let texts: Vec<String> = headings.iter().map(|n| heading_text(*n, &lines)).collect();
        assert_eq!(texts, vec!["Real".to_string(), "Also Real".to_string()]);
    }

    #[test]
    fn tilde_fences_are_recognised() {
        let src = "# Real\n\n~~~\n# inside tilde fence\n~~~\n\n## Other\n";
        let tree = parse_markdown(src).unwrap();
        let lines: Vec<&str> = src.lines().collect();
        let headings = collect_headings(&tree);
        let texts: Vec<String> = headings.iter().map(|n| heading_text(*n, &lines)).collect();
        assert_eq!(texts, vec!["Real".to_string(), "Other".to_string()]);
    }

    #[test]
    fn level_extraction_covers_h1_through_h6() {
        let src = "# A\n\n## B\n\n### C\n\n#### D\n\n##### E\n\n###### F\n";
        let tree = parse_markdown(src).unwrap();
        let headings = collect_headings(&tree);
        let levels: Vec<u8> = headings.iter().filter_map(|n| heading_level(*n)).collect();
        assert_eq!(levels, vec![1, 2, 3, 4, 5, 6]);
    }

    #[test]
    fn trailing_atx_close_hashes_are_stripped() {
        let src = "## Foo ##\n";
        let tree = parse_markdown(src).unwrap();
        let lines: Vec<&str> = src.lines().collect();
        let headings = collect_headings(&tree);
        assert_eq!(heading_text(headings[0], &lines), "Foo");
    }
}

#[cfg(test)]
mod bash_outline_tests {
    use super::{extract_import_source, get_outline_entries};
    use crate::search::callees::extract_callee_names;
    use crate::types::{Lang, OutlineKind};

    // Fixture covering both function syntaxes, top-level vars, and a nested local.
    const BASH_FIXTURE: &str = r#"MY_CONST=hello
DEBUG_MODE=0

greet() { echo "hi $1"; }

function cleanup {
    rm -f /tmp/x
}

main() {
    greet world
    cleanup
    local y=1
}
"#;

    #[test]
    fn bash_outline_functions_and_vars() {
        let entries = get_outline_entries(BASH_FIXTURE, Lang::Bash);

        let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();

        // All three functions must appear
        assert!(
            names.contains(&"greet"),
            "expected greet in outline, got: {names:?}"
        );
        assert!(
            names.contains(&"cleanup"),
            "expected cleanup in outline, got: {names:?}"
        );
        assert!(
            names.contains(&"main"),
            "expected main in outline, got: {names:?}"
        );

        // Top-level variables must appear
        assert!(
            names.contains(&"MY_CONST"),
            "expected MY_CONST in outline, got: {names:?}"
        );
        assert!(
            names.contains(&"DEBUG_MODE"),
            "expected DEBUG_MODE in outline, got: {names:?}"
        );

        // Functions must have Function kind
        for fname in &["greet", "cleanup", "main"] {
            let entry = entries.iter().find(|e| e.name == *fname).unwrap();
            assert_eq!(
                entry.kind,
                OutlineKind::Function,
                "{fname} should be OutlineKind::Function"
            );
        }

        // Variables must have Variable kind
        for vname in &["MY_CONST", "DEBUG_MODE"] {
            let entry = entries.iter().find(|e| e.name == *vname).unwrap();
            assert_eq!(
                entry.kind,
                OutlineKind::Variable,
                "{vname} should be OutlineKind::Variable"
            );
        }

        // Nested `local y=1` must NOT appear at the top level
        assert!(
            !names.contains(&"y"),
            "nested local 'y' must not appear in top-level outline, got: {names:?}"
        );
    }

    #[test]
    fn go_outline_names_const_and_var_declarations() {
        let src =
            "package x\n\nvar GlobalVar = 1\nconst MaxRetries = 3\nconst (\n\tA = 1\n\tB = 2\n)\n";
        let entries = get_outline_entries(src, Lang::Go);
        let mut got = Vec::new();
        for entry in &entries {
            got.push((entry.kind, entry.name.as_str(), entry.start_line));
        }
        assert_eq!(
            got,
            vec![
                (OutlineKind::Variable, "GlobalVar", 3),
                (OutlineKind::Constant, "MaxRetries", 4),
                (OutlineKind::Constant, "A", 5),
            ]
        );
    }

    #[test]
    fn bash_callee_names_for_main() {
        // Derive main's range from the outline so the test can't silently drift
        // if the fixture is edited.
        let main = get_outline_entries(BASH_FIXTURE, Lang::Bash)
            .into_iter()
            .find(|e| e.name == "main")
            .expect("main must be in the outline");
        let names = extract_callee_names(
            BASH_FIXTURE,
            Lang::Bash,
            Some((main.start_line, main.end_line)),
        );

        assert!(
            names.contains(&"greet".to_string()),
            "expected greet as callee, got: {names:?}"
        );
        assert!(
            names.contains(&"cleanup".to_string()),
            "expected cleanup as callee, got: {names:?}"
        );
        // echo is called inside greet, outside main's range, so it is absent here.
    }

    #[test]
    fn bash_outline_surfaces_declarations_and_hyphenated_names() {
        // export/declare/readonly declarations must surface (the common config
        // pattern), and hyphenated function names must be captured whole.
        let src = "export E_VAR=1\n\
                   declare -r D_VAR=2\n\
                   readonly R_VAR=3\n\
                   deploy-app() { :; }\n";
        let entries = get_outline_entries(src, Lang::Bash);
        let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();

        for v in ["E_VAR", "D_VAR", "R_VAR"] {
            let e = entries
                .iter()
                .find(|e| e.name == v)
                .unwrap_or_else(|| panic!("{v} should be outlined, got: {names:?}"));
            assert_eq!(e.kind, OutlineKind::Variable, "{v} should be a Variable");
        }
        let dep = entries
            .iter()
            .find(|e| e.name == "deploy-app")
            .unwrap_or_else(|| panic!("deploy-app should be outlined whole, got: {names:?}"));
        assert_eq!(dep.kind, OutlineKind::Function);
    }

    #[test]
    fn bash_extract_import_source_source_keyword() {
        let line = "source ./lib/utils.sh";
        let result = extract_import_source(line, Some(Lang::Bash));
        assert_eq!(result, "./lib/utils.sh");
    }

    #[test]
    fn bash_extract_import_source_dot_keyword() {
        let line = ". ./config.sh";
        let result = extract_import_source(line, Some(Lang::Bash));
        assert_eq!(result, "./config.sh");
    }

    #[test]
    fn bash_extract_import_source_quoted() {
        let line = r#"source "./lib/helpers.sh""#;
        let result = extract_import_source(line, Some(Lang::Bash));
        assert_eq!(result, "./lib/helpers.sh");
    }

    #[test]
    fn bash_extract_import_source_variable_expanded_returns_empty() {
        let line = r#"source "$DIR/lib.sh""#;
        let result = extract_import_source(line, Some(Lang::Bash));
        assert!(
            result.is_empty(),
            "variable-expanded source should return empty, got: {result:?}"
        );
    }

    #[test]
    fn bash_subscript_assignment_surfaces_base_name() {
        // `ARR[0]=hello` should appear as `ARR` (Variable), not `ARR[0]`.
        let entries = get_outline_entries("ARR[0]=hello\n", Lang::Bash);
        let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();
        assert!(
            names.contains(&"ARR"),
            "expected ARR in outline, got: {names:?}"
        );
        assert!(
            !names.contains(&"ARR[0]"),
            "ARR[0] must not appear verbatim in outline, got: {names:?}"
        );
        let entry = entries.iter().find(|e| e.name == "ARR").unwrap();
        assert_eq!(
            entry.kind,
            OutlineKind::Variable,
            "ARR should be OutlineKind::Variable"
        );
    }

    #[test]
    fn bash_extract_import_source_tab_separated() {
        // `source\t./lib.sh` (tab separator) must be parsed correctly.
        let result = extract_import_source("source\t./lib/utils.sh", Some(Lang::Bash));
        assert_eq!(result, "./lib/utils.sh");
    }
}

#[cfg(test)]
mod semantic_span_tests {
    use super::get_outline_entries;
    use crate::types::{Lang, OutlineKind};

    #[test]
    fn python_decorators_and_continued_headers_keep_canonical_anchor() {
        let source = "class Handler:\n\
                      \u{20}   @logged\n\
                      \u{20}   async \\\n\
                      \u{20}   def blocked(self) -> bool:\n\
                      \u{20}       return True\n";
        let entries = get_outline_entries(source, Lang::Python);
        let handler = entries
            .iter()
            .find(|entry| entry.name == "Handler")
            .expect("class should be outlined");
        let blocked = handler
            .children
            .iter()
            .find(|entry| entry.name == "blocked")
            .expect("decorated method should be outlined");

        assert_eq!(blocked.kind, OutlineKind::Function);
        assert_eq!(blocked.start_line, 4);
        assert_eq!(blocked.span_start_line, 2);
        assert_eq!(blocked.end_line, 5);
        assert_eq!(
            blocked.signature.as_deref(),
            Some("def blocked(self) -> bool")
        );
    }

    #[test]
    fn javascript_export_wrapper_keeps_inner_declaration_shape() {
        let entries = get_outline_entries("export function run() {}\n", Lang::JavaScript);
        let entry = entries
            .first()
            .expect("exported function should be outlined");
        assert_eq!(entry.kind, OutlineKind::Function);
        assert_eq!(entry.name, "run");
        assert_eq!(entry.start_line, 1);
        assert_eq!(entry.span_start_line, 1);
    }

    #[test]
    fn python_wrapper_does_not_cross_blank_or_comment_separation() {
        let source = "@first\n\
                      \n\
                      # unrelated\n\
                      @second\n\
                      def run():\n\
                          return 1\n";
        let entries = get_outline_entries(source, Lang::Python);
        let run = entries
            .iter()
            .find(|entry| entry.name == "run")
            .expect("run should be outlined");
        assert_eq!(run.start_line, 5);
        assert_eq!(run.span_start_line, 4);
    }

    #[test]
    fn annotation_bearing_languages_keep_canonical_and_semantic_lines() {
        let cases = [
            (Lang::Java, "@Deprecated\nclass Foo {}\n", "Foo", 2, 1),
            (Lang::CSharp, "[Obsolete]\nclass Foo {}\n", "Foo", 2, 1),
            (
                Lang::Kotlin,
                "@Deprecated(\"old\")\nclass Foo {}\n",
                "Foo",
                2,
                1,
            ),
            (Lang::TypeScript, "@sealed\nclass Foo {}\n", "Foo", 2, 1),
            (Lang::Tsx, "@sealed\nclass Foo {}\n", "Foo", 2, 1),
            (Lang::JavaScript, "@sealed\nclass Foo {}\n", "Foo", 2, 1),
            (
                Lang::Php,
                "<?php\n#[Attr]\nfunction run() {}\n",
                "run",
                3,
                2,
            ),
            (
                Lang::Python,
                "@logged\ndef run():\n    return 1\n",
                "run",
                2,
                1,
            ),
            (Lang::Rust, "#[inline]\nfn run() {}\n", "run", 2, 1),
            (Lang::Scala, "@deprecated\nclass Foo\n", "Foo", 2, 1),
            (
                Lang::Swift,
                "@available(*, deprecated)\nfunc run() {}\n",
                "run",
                2,
                1,
            ),
            (
                Lang::C,
                "[[nodiscard]]\nint run() { return 0; }\n",
                "run",
                2,
                1,
            ),
            (
                Lang::Cpp,
                "[[nodiscard]]\nint run() { return 0; }\n",
                "run",
                2,
                1,
            ),
        ];

        for (lang, source, name, start_line, span_start_line) in cases {
            let entries = get_outline_entries(source, lang);
            let entry = entries
                .iter()
                .find(|entry| entry.name == name)
                .unwrap_or_else(|| panic!("{lang:?} should outline {name}"));
            assert_eq!(entry.start_line, start_line, "{lang:?} canonical line");
            assert_eq!(
                entry.span_start_line, span_start_line,
                "{lang:?} semantic line"
            );
        }
    }
    #[test]
    fn deep_outline_handles_pathological_ast_depth_iteratively() {
        let depth = 1_000;
        let source = format!(
            "void run() {{\n{}{}{}\n",
            "{\n".repeat(depth),
            "}\n".repeat(depth),
            "}\n",
        );
        let entries = super::get_deep_outline_entries(&source, Lang::C);
        assert!(entries.iter().any(|entry| entry.name == "run"));
    }

    #[test]
    fn embedded_adornments_stop_at_blank_and_comment_gaps() {
        let cases = [
            (
                Lang::C,
                "[[nodiscard]]\n\nint run() { return 0; }\n",
                "run",
                3,
                3,
            ),
            (
                Lang::Cpp,
                "[[nodiscard]]\n\nint run() { return 0; }\n",
                "run",
                3,
                3,
            ),
            (
                Lang::Java,
                "@First // unrelated\nclass Foo {}\n",
                "Foo",
                2,
                2,
            ),
            (
                Lang::CSharp,
                "[First] // unrelated\nclass Foo {}\n",
                "Foo",
                2,
                2,
            ),
            (
                Lang::Java,
                "@First\n\n// unrelated\n@Second\nclass Foo {}\n",
                "Foo",
                5,
                4,
            ),
            (
                Lang::CSharp,
                "[First]\n\n/* unrelated */\n[Second]\nclass Foo {}\n",
                "Foo",
                5,
                4,
            ),
            (
                Lang::Kotlin,
                "@First\n\n// unrelated\n@Second\nclass Foo {}\n",
                "Foo",
                5,
                4,
            ),
            (
                Lang::Scala,
                "@First\n\n// unrelated\n@Second\nclass Foo {}\n",
                "Foo",
                5,
                4,
            ),
            (
                Lang::Swift,
                "@available(*, deprecated)\n\n// unrelated\n@available(*, unavailable)\nclass Foo {}\n",
                "Foo",
                5,
                4,
            ),
            (
                Lang::TypeScript,
                "@first\n\n// unrelated\n@second\nclass Foo {}\n",
                "Foo",
                5,
                4,
            ),
            (
                Lang::Php,
                "<?php\n#[First]\n\n/* unrelated */\n#[Second]\nclass Foo {}\n",
                "Foo",
                6,
                5,
            ),
        ];

        for (lang, source, name, start_line, span_start_line) in cases {
            let entry = get_outline_entries(source, lang)
                .into_iter()
                .find(|entry| entry.name == name)
                .unwrap_or_else(|| panic!("{lang:?} should outline {name}"));
            assert_eq!(entry.start_line, start_line, "{lang:?} keyword line");
            assert_eq!(
                entry.span_start_line, span_start_line,
                "{lang:?} should keep only contiguous adornments"
            );
        }
    }

    #[test]
    fn keyword_anchor_survives_name_on_following_line() {
        let entries = get_outline_entries("@sealed\nclass\nFoo {}\n", Lang::TypeScript);
        let entry = entries
            .iter()
            .find(|entry| entry.name == "Foo")
            .expect("split class should be outlined");
        assert_eq!(entry.start_line, 2);
        assert_eq!(entry.span_start_line, 1);
    }

    #[test]
    fn elixir_dialyzer_is_not_positional_definition_metadata() {
        let source = "defmodule M do\n\
                      @dialyzer {:nowarn_function, helper: 0}\n\
                      def run, do: :ok\n\
                      end\n";
        let entries = get_outline_entries(source, Lang::Elixir);
        let entry = super::find_entry_by_start_line(&entries, 3).expect("run should be outlined");
        assert_eq!(entry.start_line, 3);
        assert_eq!(entry.span_start_line, 3);
    }

    #[test]
    fn rust_outer_attributes_extend_only_the_next_declaration() {
        let source = "#[inline]\n\
                      #[cfg(test)]\n\
                      fn run() {}\n\
                      \n\
                      #[cold]\n\
                      // separated from the next declaration\n\
                      fn cold_run() {}\n";
        let entries = get_outline_entries(source, Lang::Rust);
        let run = entries
            .iter()
            .find(|entry| entry.name == "run")
            .expect("run should be outlined");
        let cold_run = entries
            .iter()
            .find(|entry| entry.name == "cold_run")
            .expect("cold_run should be outlined");

        assert_eq!(run.start_line, 3);
        assert_eq!(run.span_start_line, 1);
        assert_eq!(cold_run.start_line, 7);
        assert_eq!(cold_run.span_start_line, 7);
    }

    #[test]
    fn elixir_definition_metadata_is_conservative_and_contiguous() {
        let source = "@doc \"run\"\n\
                      @spec run(integer()) :: integer()\n\
                      def run(value), do: value\n\
                      @custom true\n\
                      def other, do: :ok\n\
                      @doc \"separated\"\n\
                      \n\
                      def split, do: :ok\n";
        let entries = get_outline_entries(source, Lang::Elixir);
        let run = entries
            .iter()
            .find(|entry| entry.name == "run")
            .expect("run should be outlined");
        let other = entries
            .iter()
            .find(|entry| entry.name == "other")
            .expect("other should be outlined");
        let split = entries
            .iter()
            .find(|entry| entry.name == "split")
            .expect("split should be outlined");

        assert_eq!(run.start_line, 3);
        assert_eq!(run.span_start_line, 1);
        assert_eq!(other.span_start_line, 5);
        assert_eq!(split.span_start_line, 8);
    }
}
