//! Shared tree-sitter utilities used by symbol search and caller search.

use std::collections::{HashMap, HashSet};
use std::ops::ControlFlow;
use std::sync::{Arc, LazyLock, Mutex};

use ast_grep_core::matcher::PatternBuilder;
use ast_grep_core::source::Edit;
use ast_grep_core::tree_sitter::{LanguageExt, StrDoc};
use ast_grep_core::{AstGrep, Language, Pattern, PatternError};

/// The grammar identity for an ast-grep-owned parsed document.
#[derive(Clone)]
pub(crate) struct DocumentLanguage(tree_sitter::Language);

impl Language for DocumentLanguage {
    fn kind_to_id(&self, kind: &str) -> u16 {
        self.0.id_for_node_kind(kind, true)
    }

    fn field_to_id(&self, field: &str) -> Option<u16> {
        self.0
            .field_id_for_name(field)
            .map(std::num::NonZeroU16::get)
    }

    fn build_pattern(&self, builder: &PatternBuilder) -> Result<Pattern, PatternError> {
        builder.build(|source| StrDoc::try_new(source, self.clone()))
    }
}

impl LanguageExt for DocumentLanguage {
    fn get_ts_language(&self) -> tree_sitter::Language {
        self.0.clone()
    }
}

pub(crate) type ParsedDocument = AstGrep<StrDoc<DocumentLanguage>>;
pub(crate) type DocumentNode<'r> = ast_grep_core::Node<'r, StrDoc<DocumentLanguage>>;

/// Parse source without retaining a parser or a global lock.
pub(crate) fn parse_source(
    content: &str,
    language: &tree_sitter::Language,
) -> Option<tree_sitter::Tree> {
    record_parse(content, false);
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(language).ok()?;
    let tree = parser.parse(content, None);
    pause_parse(content);
    tree
}

/// Create the ast-grep document that owns one immutable parsed snapshot.
/// An owned `String` moves into the document without a copy.
pub(crate) fn parse_document(
    content: impl Into<String>,
    language: &tree_sitter::Language,
) -> Option<ParsedDocument> {
    let src = content.into();
    let tree = parse_source(&src, language)?;
    Some(AstGrep::doc(StrDoc {
        src,
        lang: DocumentLanguage(language.clone()),
        tree,
    }))
}

/// Clone one immutable snapshot and delegate its edit and reparse to ast-grep.
pub(crate) fn document_after_edit(
    before: &str,
    after: &str,
    document: &ParsedDocument,
) -> Option<ParsedDocument> {
    debug_assert_eq!(document.root().get_doc().src, before);
    let mut start = before
        .bytes()
        .zip(after.bytes())
        .take_while(|(left, right)| left == right)
        .count();
    while !before.is_char_boundary(start) || !after.is_char_boundary(start) {
        start -= 1;
    }
    let mut suffix = before[start..]
        .bytes()
        .rev()
        .zip(after[start..].bytes().rev())
        .take_while(|(left, right)| left == right)
        .count();
    while !before.is_char_boundary(before.len() - suffix)
        || !after.is_char_boundary(after.len() - suffix)
    {
        suffix -= 1;
    }
    let old_end = before.len() - suffix;
    let new_end = after.len() - suffix;
    let mut edited = document.clone();
    record_parse(after, true);
    let result = edited.edit(Edit {
        position: start,
        deleted_length: old_end - start,
        inserted_text: after.as_bytes()[start..new_end].to_vec(),
    });
    pause_parse(after);
    result.ok()?;
    Some(edited)
}

fn record_parse(content: &str, incremental: bool) {
    #[cfg(test)]
    if let Some(counts) = PARSE_COUNTS.lock().unwrap().get_mut(content) {
        if incremental {
            counts.incremental += 1;
        } else {
            counts.full += 1;
        }
    }
    #[cfg(not(test))]
    let _ = (content, incremental);
}

fn pause_parse(content: &str) {
    #[cfg(test)]
    {
        let pause = PARSE_PAUSES.lock().unwrap().remove(content);
        if let Some((entered, resume)) = pause {
            entered.send(()).unwrap();
            resume
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }
    }
    #[cfg(not(test))]
    let _ = content;
}

#[cfg(test)]
#[derive(Default)]
struct ParseCounts {
    full: usize,
    incremental: usize,
    scanned: Vec<usize>,
}

#[cfg(test)]
pub(crate) fn record_scanned_document(content: &str, ptr: usize) {
    if let Some(counts) = PARSE_COUNTS.lock().unwrap().get_mut(content) {
        counts.scanned.push(ptr);
    }
}

#[cfg(test)]
static PARSE_COUNTS: LazyLock<Mutex<HashMap<String, ParseCounts>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[cfg(test)]
type ParsePause = (std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>);
#[cfg(test)]
static PARSE_PAUSES: LazyLock<Mutex<HashMap<String, ParsePause>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Counts real parses of unique fixture bytes, including parallel walker threads.
#[cfg(test)]
pub(crate) struct ParseWitness(String);

#[cfg(test)]
impl ParseWitness {
    pub(crate) fn new(content: &str) -> Self {
        assert!(PARSE_COUNTS
            .lock()
            .unwrap()
            .insert(content.to_string(), ParseCounts::default())
            .is_none());
        Self(content.to_string())
    }

    pub(crate) fn count(&self) -> usize {
        PARSE_COUNTS.lock().unwrap()[&self.0].full
    }

    pub(crate) fn incremental_count(&self) -> usize {
        PARSE_COUNTS.lock().unwrap()[&self.0].incremental
    }

    pub(crate) fn scanned_pointers(&self) -> Vec<usize> {
        PARSE_COUNTS.lock().unwrap()[&self.0].scanned.clone()
    }

    pub(crate) fn pause_next(
        &self,
    ) -> (std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>) {
        let (entered, observed) = std::sync::mpsc::channel();
        let (resume, release) = std::sync::mpsc::channel();
        assert!(PARSE_PAUSES
            .lock()
            .unwrap()
            .insert(self.0.clone(), (entered, release))
            .is_none());
        (observed, resume)
    }
}

#[cfg(test)]
impl Drop for ParseWitness {
    fn drop(&mut self) {
        PARSE_COUNTS.lock().unwrap().remove(&self.0);
        PARSE_PAUSES.lock().unwrap().remove(&self.0);
    }
}

/// Definition node kinds across tree-sitter grammars.
pub(crate) const DEFINITION_KINDS: &[&str] = &[
    // Functions
    "function_declaration",
    "function_definition",
    "function_item",
    "method_definition",
    "method_declaration",
    "method",           // Ruby
    "singleton_method", // Ruby
    // Classes, structs & Kotlin objects
    "class_declaration",
    "class_definition",
    "class",           // Ruby
    "class_specifier", // C++
    "struct_item",
    "struct_specifier", // C++
    "object_declaration",
    // Interfaces & types (TS)
    "interface_declaration",
    "trait_declaration",
    "type_alias_declaration",
    "type_item",
    // Enums
    "enum_item",
    "enum_declaration",
    // Variables, constants & properties (Kotlin, C#, Swift)
    "lexical_declaration",
    "variable_declaration",
    "variable_assignment", // Bash top-level assignments (bash-only today; a future grammar reusing this node kind would inherit definition_weight 60)
    "const_item",
    "const_declaration",
    "static_item",
    "property_declaration",
    // Rust-specific
    "trait_item",
    "impl_item",
    "mod_item",
    "namespace_definition",
    "internal_module",
    // Python
    "decorated_definition",
    "type_declaration",
    "var_declaration",
    // Exports
    "export_statement",
];

/// Extract the name defined by a tree-sitter definition node.
///
/// Walks standard field names (`name`, `identifier`, `declarator`) and handles
/// nested declarators and export statements.
pub(crate) fn extract_definition_name(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    // Try standard field names
    for field in &["name", "identifier", "declarator"] {
        if let Some(child) = node.child_by_field_name(field) {
            let text = node_text_simple(child, lines, NodeTextMode::Full);
            if !text.is_empty() {
                if child.kind().contains("declarator") {
                    if let Some(name) = declarator_name(child, lines) {
                        return Some(name);
                    }
                }
                return Some(text);
            }
        }
    }

    // For export_statement, check the declaration child
    if node.kind() == "export_statement" {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if DEFINITION_KINDS.contains(&child.kind()) {
                return extract_definition_name(child, lines);
            }
        }
    }

    let kind = node.kind();
    if kind == "type_declaration" || kind == "const_declaration" || kind == "var_declaration" {
        if let Some(name) = go_spec_name(node, lines) {
            return Some(name);
        }
    }

    // JS/TS `lexical_declaration` and C# `variable_declaration` store the
    // identifier inside a `variable_declarator` child (field "declarations" /
    // unnamed children), not as a direct named field on the declaration node.
    // Walk children to find the first `variable_declarator` and pull its `name`.
    if node.kind() == "lexical_declaration" || node.kind() == "variable_declaration" {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "variable_declarator" {
                if let Some(name_node) = child.child_by_field_name("name") {
                    let text = node_text_simple(name_node, lines, NodeTextMode::Full);
                    if !text.is_empty() {
                        return Some(text);
                    }
                }
            }
        }
    }

    None
}

fn declarator_name(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let mut pending = vec![node];
    while let Some(current) = pending.pop() {
        if matches!(
            current.kind(),
            "identifier" | "field_identifier" | "operator_name"
        ) {
            return Some(node_text_simple(current, lines, NodeTextMode::Full));
        }
        if current.kind() == "parenthesized_declarator" {
            if let Some(child) = current.named_child(0) {
                pending.push(child);
            }
        }
        for field in ["name", "identifier", "declarator"].into_iter().rev() {
            if let Some(child) = current.child_by_field_name(field) {
                pending.push(child);
            }
        }
    }
    None
}

fn go_spec_name(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        let kind = child.kind();
        if kind == "type_spec" || kind == "type_alias" || kind == "const_spec" || kind == "var_spec"
        {
            let Some(name_node) = child.child_by_field_name("name") else {
                continue;
            };
            let text = node_text_simple(name_node, lines, NodeTextMode::Full);
            if !text.is_empty() {
                return Some(text);
            }
        }
        if kind == "var_spec_list" {
            if let Some(name) = go_spec_name(child, lines) {
                return Some(name);
            }
        }
    }
    None
}

pub(crate) fn go_declaration_name_line(
    node: tree_sitter::Node,
    lines: &[&str],
    query: &str,
) -> Option<u32> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        let kind = child.kind();
        if kind == "type_spec" || kind == "type_alias" || kind == "const_spec" || kind == "var_spec"
        {
            let mut names = child.walk();
            for name_node in child.children_by_field_name("name", &mut names) {
                if node_text_simple(name_node, lines, NodeTextMode::Full) == query {
                    return Some(name_node.start_position().row as u32 + 1);
                }
            }
        }
        if kind == "var_spec_list" {
            if let Some(line) = go_declaration_name_line(child, lines, query) {
                return Some(line);
            }
        }
    }
    None
}

/// Controls how [`node_text_simple`] renders a node that spans multiple lines.
#[derive(Clone, Copy)]
pub(crate) enum NodeTextMode {
    /// Return the node's start line untruncated.
    Full,
    /// Truncate the node's start line to roughly 80 characters.
    Truncated,
}

/// Returns a node's text from pre-split source lines.
///
/// For a single-line node, returns its exact slice. For a multi-line node,
/// returns only the start line (start column to end-of-line) — never the full
/// multi-line span; `mode` then decides whether that start line is returned in
/// full or truncated.
pub(crate) fn node_text_simple(
    node: tree_sitter::Node,
    lines: &[&str],
    mode: NodeTextMode,
) -> String {
    let row = node.start_position().row;
    let col_start = node.start_position().column;
    let end_row = node.end_position().row;
    if row < lines.len() && row == end_row {
        let col_end = node.end_position().column.min(lines[row].len());
        lines[row][col_start..col_end].to_string()
    } else if row < lines.len() {
        let text = &lines[row][col_start..];
        match mode {
            NodeTextMode::Full => text.to_string(),
            NodeTextMode::Truncated => {
                if text.len() > 80 {
                    format!("{}...", crate::types::truncate_str(text, 77))
                } else {
                    text.to_string()
                }
            }
        }
    } else {
        String::new()
    }
}

/// Extract trait name from Rust `impl Trait for Type` node.
/// Returns None for inherent impls (no trait).
pub(crate) fn extract_impl_trait(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let trait_node = node.child_by_field_name("trait")?;
    Some(node_text_simple(trait_node, lines, NodeTextMode::Full))
}

/// Extract implementing type from Rust `impl ... for Type` node.
pub(crate) fn extract_impl_type(node: tree_sitter::Node, lines: &[&str]) -> Option<String> {
    let type_node = node.child_by_field_name("type")?;
    Some(node_text_simple(type_node, lines, NodeTextMode::Full))
}

/// Extract implemented interface names from TypeScript and Java classes.
///
/// Only each top-level interface type contributes a name. Generic arguments
/// are not themselves implemented interfaces.
pub(crate) fn extract_implemented_interfaces(
    node: tree_sitter::Node,
    lines: &[&str],
) -> Vec<String> {
    let mut interfaces = Vec::new();
    let mut seen = HashSet::new();
    collect_implemented_clauses(node, lines, &mut interfaces, &mut seen);
    interfaces
}

fn collect_implemented_clauses(
    node: tree_sitter::Node,
    lines: &[&str],
    interfaces: &mut Vec<String>,
    seen: &mut HashSet<String>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "implements_clause" || child.kind() == "super_interfaces" {
            collect_interface_types(child, lines, interfaces, seen);
        } else if child.kind() == "class_heritage" {
            collect_implemented_clauses(child, lines, interfaces, seen);
        }
    }
}

fn collect_interface_types(
    node: tree_sitter::Node,
    lines: &[&str],
    interfaces: &mut Vec<String>,
    seen: &mut HashSet<String>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if matches!(
            child.kind(),
            "implements_clause" | "super_interfaces" | "type_list"
        ) {
            collect_interface_types(child, lines, interfaces, seen);
            continue;
        }
        let text = node_text_simple(child, lines, NodeTextMode::Full);
        let base = text.split('<').next().unwrap_or_default();
        let name = base
            .rsplit(['.', ':'])
            .next()
            .unwrap_or_default()
            .split_whitespace()
            .collect::<String>();
        if !name.is_empty() && seen.insert(name.clone()) {
            interfaces.push(name);
        }
    }
}

// ---------------------------------------------------------------------------
// Elixir-specific definition helpers
// ---------------------------------------------------------------------------

/// Find the `arguments` child of an Elixir `call` node.
/// In tree-sitter-elixir, `arguments` is a node kind, not a named field,
/// so `child_by_field_name("arguments")` doesn't work.
pub(crate) fn elixir_arguments(node: tree_sitter::Node) -> Option<tree_sitter::Node> {
    let mut cursor = node.walk();
    // Node is Copy (arena index) — the returned node survives cursor drop.
    let result = node.children(&mut cursor).find(|c| c.kind() == "arguments");
    result
}

/// Extract function name from the first argument of a `def`/`defp`/`defmacro` call.
///
/// The first argument can be:
/// - `call` node: `def greet(name)` → target is `greet`
/// - `identifier` node: `def bar, do: :ok` → text is `bar`
/// - `binary_operator` with `when`: `def foo(x) when x > 0` → unwrap left, then recurse
pub(crate) fn elixir_extract_func_head_name(
    node: tree_sitter::Node,
    lines: &[&str],
) -> Option<String> {
    match node.kind() {
        "call" => node
            .child_by_field_name("target")
            .map(|t| node_text_simple(t, lines, NodeTextMode::Full)),
        "identifier" => Some(node_text_simple(node, lines, NodeTextMode::Full)),
        "binary_operator" => {
            // Guard clause: `foo(x) when x > 0` → left is the function head
            let left = node.child_by_field_name("left")?;
            elixir_extract_func_head_name(left, lines)
        }
        _ => None,
    }
}

/// Semantic weight for definition kinds. Primary declarations rank highest.
pub(crate) fn definition_weight(kind: &str) -> u16 {
    match kind {
        "function_declaration"
        | "function_definition"
        | "function_item"
        | "method_definition"
        | "method_declaration"
        | "method"
        | "singleton_method"
        | "class_declaration"
        | "class_definition"
        | "class"
        | "class_specifier"
        | "struct_item"
        | "struct_specifier"
        | "interface_declaration"
        | "trait_declaration"
        | "trait_item"
        | "enum_item"
        | "enum_declaration"
        | "type_item"
        | "type_declaration"
        | "decorated_definition" => 100,
        "impl_item" | "object_declaration" => 90,
        "const_item" | "const_declaration" | "var_declaration" | "static_item" => 80,
        "mod_item" | "namespace_definition" | "property_declaration" => 70,
        "lexical_declaration" | "variable_declaration" => 40,
        "variable_assignment" => 60,
        "export_statement" => 30,
        _ => 50,
    }
}

/// Global cache of compiled tree-sitter queries. The key uses tree-sitter's
/// grammar identity and query content, including grammars without a name.
type QueryKey = (tree_sitter::Language, &'static str);
static QUERY_CACHE: LazyLock<Mutex<HashMap<QueryKey, Arc<tree_sitter::Query>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[cfg(test)]
static QUERY_COMPILE_ATTEMPTS: LazyLock<Mutex<HashMap<QueryKey, usize>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Look up or compile `query_str` for `ts_lang`, then invoke `f` with a
/// reference to the cached `Query`. Returns `None` if compilation fails.
fn with_query<R>(
    ts_lang: &tree_sitter::Language,
    query_str: &'static str,
    f: impl FnOnce(&tree_sitter::Query) -> R,
) -> Option<R> {
    use std::collections::hash_map::Entry;

    let key = (ts_lang.clone(), query_str);
    let mut cache = QUERY_CACHE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let query = match cache.entry(key) {
        Entry::Occupied(entry) => Arc::clone(entry.get()),
        Entry::Vacant(entry) => {
            #[cfg(test)]
            {
                let mut attempts = QUERY_COMPILE_ATTEMPTS
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                *attempts.entry((ts_lang.clone(), query_str)).or_default() += 1;
            }
            let query = Arc::new(tree_sitter::Query::new(ts_lang, query_str).ok()?);
            Arc::clone(entry.insert(query))
        }
    };
    drop(cache);
    Some(f(&query))
}

/// Run the cached query over `root` and give each match to `visit`, in order.
/// Each match holds the first node of each capture in `names`.
/// `visit` returns `ControlFlow::Break` to stop the walk.
/// A query that does not compile gives no matches.
pub(crate) fn visit_query_captures<'tree, const N: usize>(
    ts_lang: &tree_sitter::Language,
    query_str: &'static str,
    root: tree_sitter::Node<'tree>,
    source: &[u8],
    names: [&str; N],
    mut visit: impl FnMut([Option<tree_sitter::Node<'tree>>; N]) -> ControlFlow<()>,
) {
    use streaming_iterator::StreamingIterator;

    with_query(ts_lang, query_str, |query| {
        let indices = names.map(|name| query.capture_index_for_name(name));
        let mut cursor = tree_sitter::QueryCursor::new();
        let mut matches = cursor.matches(query, root, source);
        while let Some(found_match) = matches.next() {
            let nodes = indices.map(|index| {
                let index = index?;
                let capture = found_match.captures().iter().find(|c| c.index == index)?;
                Some(capture.node)
            });
            if visit(nodes).is_break() {
                break;
            }
        }
    });
}

/// Collect every match of [`visit_query_captures`].
pub(crate) fn query_captures<'tree, const N: usize>(
    ts_lang: &tree_sitter::Language,
    query_str: &'static str,
    root: tree_sitter::Node<'tree>,
    source: &[u8],
    names: [&str; N],
) -> Vec<[Option<tree_sitter::Node<'tree>>; N]> {
    let mut found = Vec::new();
    visit_query_captures(ts_lang, query_str, root, source, names, |nodes| {
        found.push(nodes);
        ControlFlow::Continue(())
    });
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    fn captures(
        query: &tree_sitter::Query,
        tree: &tree_sitter::Tree,
        source: &str,
        capture_name: &str,
    ) -> Vec<String> {
        use streaming_iterator::StreamingIterator;

        let capture = query
            .capture_index_for_name(capture_name)
            .expect("query has the requested capture");
        let mut cursor = tree_sitter::QueryCursor::new();
        let mut matches = cursor.matches(query, tree.root_node(), source.as_bytes());
        let mut names = Vec::new();
        while let Some(query_match) = matches.next() {
            names.extend(
                query_match
                    .captures()
                    .iter()
                    .filter(|candidate| candidate.index == capture)
                    .map(|candidate| {
                        candidate
                            .node
                            .utf8_text(source.as_bytes())
                            .expect("capture is valid UTF-8")
                            .to_owned()
                    }),
            );
        }
        names
    }

    fn query_compile_attempts(language: &tree_sitter::Language, query: &'static str) -> usize {
        QUERY_COMPILE_ATTEMPTS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(&(language.clone(), query))
            .copied()
            .unwrap_or_default()
    }

    #[test]
    fn shared_query_cache_distinguishes_query_content() {
        const FUNCTION_QUERY: &str = "(function_item name: (identifier) @target)";
        const CALL_QUERY: &str = "(call_expression function: (identifier) @target)";
        const SOURCE: &str = "fn alpha() { beta(); }";

        let language: tree_sitter::Language = tree_sitter_rust::LANGUAGE.into();
        let tree = parse_source(SOURCE, &language).expect("source parses");

        for _ in 0..2 {
            let functions = with_query(&language, FUNCTION_QUERY, |query| {
                captures(query, &tree, SOURCE, "target")
            })
            .expect("function query compiles");
            let calls = with_query(&language, CALL_QUERY, |query| {
                captures(query, &tree, SOURCE, "target")
            })
            .expect("call query compiles");
            assert_eq!(functions, ["alpha"]);
            assert_eq!(calls, ["beta"]);
        }

        assert_eq!(query_compile_attempts(&language, FUNCTION_QUERY), 1);
        assert_eq!(query_compile_attempts(&language, CALL_QUERY), 1);
    }

    #[test]
    fn shared_query_cache_uses_unnamed_grammar_identity() {
        const QUERY: &str = "(identifier) @grammar_identity_target";
        const RUST_SOURCE: &str = "fn rust_marker() {}";
        const KOTLIN_SOURCE: &str = "package kotlinMarker";

        let rust: tree_sitter::Language = tree_sitter_rust::LANGUAGE.into();
        let kotlin: tree_sitter::Language = tree_sitter_kotlin_sg::LANGUAGE.into();
        assert_eq!(kotlin.name(), None);

        let rust_tree = parse_source(RUST_SOURCE, &rust).expect("rust source parses");
        let kotlin_tree = parse_source(KOTLIN_SOURCE, &kotlin).expect("kotlin source parses");
        let rust_names = with_query(&rust, QUERY, |query| {
            captures(query, &rust_tree, RUST_SOURCE, "grammar_identity_target")
        })
        .expect("rust query compiles");
        let kotlin_names = with_query(&kotlin, QUERY, |query| {
            captures(
                query,
                &kotlin_tree,
                KOTLIN_SOURCE,
                "grammar_identity_target",
            )
        })
        .expect("kotlin query compiles");

        assert_eq!(rust_names, ["rust_marker"]);
        assert_eq!(kotlin_names, ["kotlinMarker"]);
        assert_eq!(query_compile_attempts(&rust, QUERY), 1);
        assert_eq!(query_compile_attempts(&kotlin, QUERY), 1);
    }

    #[test]
    fn shared_query_compile_failure_is_retried() {
        const INVALID_QUERY: &str = "(shared_query_missing_node) @invalid";
        let language: tree_sitter::Language = tree_sitter_rust::LANGUAGE.into();

        assert!(with_query(&language, INVALID_QUERY, |_| ()).is_none());
        assert!(with_query(&language, INVALID_QUERY, |_| ()).is_none());
        assert_eq!(query_compile_attempts(&language, INVALID_QUERY), 2);
    }

    #[test]
    fn shared_query_survives_edits_and_allows_reentrant_callbacks() {
        const OUTER_QUERY: &str = "(function_item name: (identifier) @edit_target)";
        const INNER_QUERY: &str = "(call_expression function: (identifier) @nested_target)";
        const BEFORE: &str = "fn before() { first(); }";
        const AFTER: &str = "fn after() { second(); }";

        let dir = tempfile::tempdir().expect("temporary directory is created");
        let path = dir.path().join("query-cache.rs");
        std::fs::write(&path, BEFORE).expect("initial source is written");
        let cache = crate::cache::OutlineCache::new();
        let before = cache.get_or_parse(&path).expect("initial source parses");
        let language: tree_sitter::Language = tree_sitter_rust::LANGUAGE.into();

        let (before_functions, before_calls) = with_query(&language, OUTER_QUERY, |query| {
            let functions = captures(query, before.tree(), before.content(), "edit_target");
            let calls = with_query(&language, INNER_QUERY, |nested| {
                captures(nested, before.tree(), before.content(), "nested_target")
            })
            .expect("reentrant query compiles");
            (functions, calls)
        })
        .expect("outer query compiles");
        assert_eq!(before_functions, ["before"]);
        assert_eq!(before_calls, ["first"]);

        crate::util::atomic_write_bytes(&path, AFTER.as_bytes()).expect("edit is written");
        cache.update_after_write(&path, BEFORE, AFTER);
        let after = cache.get_or_parse(&path).expect("edited source parses");
        let after_functions = with_query(&language, OUTER_QUERY, |query| {
            captures(query, after.tree(), after.content(), "edit_target")
        })
        .expect("outer query stays cached");
        let after_calls = with_query(&language, INNER_QUERY, |query| {
            captures(query, after.tree(), after.content(), "nested_target")
        })
        .expect("inner query stays cached");

        assert_eq!(after_functions, ["after"]);
        assert_eq!(after_calls, ["second"]);
        assert_eq!(query_compile_attempts(&language, OUTER_QUERY), 1);
        assert_eq!(query_compile_attempts(&language, INNER_QUERY), 1);
    }

    #[test]
    fn parse_document_moves_owned_string_without_copy() {
        let language = crate::lang::outline::outline_language(crate::types::Lang::Rust).unwrap();
        let source = String::from("fn moved() {}\n");
        let before = source.as_ptr();
        let document = parse_document(source, &language).unwrap();
        assert_eq!(document.root().get_doc().src.as_ptr(), before);
    }

    #[test]
    fn incremental_reparse_delegates_to_ast_grep_document() {
        let implementation = include_str!("treesitter.rs");
        let local_input_edit = ["tree_sitter::Input", "Edit"].concat();
        let delegated_edit = [".edit(", "Edit {"].concat();
        assert!(
            !implementation.contains(&local_input_edit),
            "local InputEdit plumbing bypasses ast-grep document edits"
        );
        assert!(
            implementation.contains(&delegated_edit),
            "incremental reparsing must call the ast-grep document edit API"
        );
    }

    #[test]
    fn node_text_mode_controls_multiline_truncation() {
        let first_line =
            "pub fn very_long_function_name_with_enough_characters_to_force_truncation_for_outline_test() {";
        let source = format!("{first_line}\n}}\n");
        let lines: Vec<&str> = source.lines().collect();
        let mut parser = tree_sitter::Parser::new();
        parser
            .set_language(&tree_sitter_rust::LANGUAGE.into())
            .expect("rust parser should load");
        let tree = parser
            .parse(&source, None)
            .expect("rust source should parse");
        let node = tree
            .root_node()
            .named_child(0)
            .expect("source should contain a function node");

        assert_eq!(
            node_text_simple(node, &lines, NodeTextMode::Full),
            first_line
        );
        assert_eq!(
            node_text_simple(node, &lines, NodeTextMode::Truncated),
            format!("{}...", &first_line[..77])
        );
    }

    use crate::lang::outline::outline_language;
    use crate::types::Lang;

    #[test]
    fn definition_weight_covers_every_tier() {
        // 100 — primary declarations, one per source language shape
        // (Rust function_item/enum_item, TS class_declaration/interface_declaration, Python decorated_definition).
        assert_eq!(definition_weight("function_item"), 100);
        assert_eq!(definition_weight("class_declaration"), 100);
        assert_eq!(definition_weight("interface_declaration"), 100);
        assert_eq!(definition_weight("enum_item"), 100);
        assert_eq!(definition_weight("decorated_definition"), 100);
        assert_eq!(definition_weight("method"), 100);
        // 90 — impls / object-like declarations (Rust impl_item, Kotlin object_declaration).
        assert_eq!(definition_weight("impl_item"), 90);
        assert_eq!(definition_weight("object_declaration"), 90);
        // 80 — const/static.
        assert_eq!(definition_weight("const_item"), 80);
        assert_eq!(definition_weight("static_item"), 80);
        // 70 — module/namespace/property.
        assert_eq!(definition_weight("mod_item"), 70);
        assert_eq!(definition_weight("property_declaration"), 70);
        // 60 — Bash top-level assignment (special-cased above the 40 tier).
        assert_eq!(definition_weight("variable_assignment"), 60);
        // 40 — plain variable declarations (JS/TS lexical_declaration, C#/Kotlin variable_declaration).
        assert_eq!(definition_weight("lexical_declaration"), 40);
        assert_eq!(definition_weight("variable_declaration"), 40);
        // 30 — export wrapper (unwrapped recursively by extract_definition_name).
        assert_eq!(definition_weight("export_statement"), 30);
        // 50 — unrecognized kind falls to the default tier, not 0.
        assert_eq!(definition_weight("comment"), 50);
    }

    #[test]
    fn deeply_nested_c_declarator_has_the_owned_name() {
        let depth = 5_000;
        let source = format!(
            "int {}deep_marker{}(void) {{ return 0; }}\n",
            "(".repeat(depth),
            ")".repeat(depth)
        );
        let tree = parse(&source, Lang::C);
        let lines: Vec<&str> = source.lines().collect();
        let node = find_by_kind(tree.root_node(), "function_definition");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("deep_marker".to_string())
        );
    }

    /// Parse `src` with `lang`'s grammar and return the owned tree.
    fn parse(src: &str, lang: Lang) -> tree_sitter::Tree {
        let language = outline_language(lang).expect("grammar available for test language");
        let mut parser = tree_sitter::Parser::new();
        parser.set_language(&language).expect("grammar loads");
        parser.parse(src, None).expect("parse succeeds")
    }

    /// Depth-first search for the first descendant node of the given kind.
    fn find_by_kind<'a>(root: tree_sitter::Node<'a>, kind: &str) -> tree_sitter::Node<'a> {
        let mut cursor = root.walk();
        let mut stack = vec![root];
        while let Some(node) = stack.pop() {
            if node.kind() == kind {
                return node;
            }
            stack.extend(node.children(&mut cursor));
        }
        panic!("no {kind} node found in parsed tree");
    }

    #[test]
    fn extract_definition_name_rust_function_item() {
        let src = "fn greet(name: &str) -> String { name.to_string() }\n";
        let tree = parse(src, Lang::Rust);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "function_item");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("greet".to_string())
        );
    }

    #[test]
    fn extract_definition_name_python_class_definition() {
        let src = "class Widget:\n    pass\n";
        let tree = parse(src, Lang::Python);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "class_definition");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("Widget".to_string())
        );
    }

    #[test]
    fn extract_definition_name_go_type_declaration_struct() {
        // Go type_declaration carries no name field itself — the name lives on
        // the inner type_spec. Without the descent every Go type definition is
        // dropped as nameless (the gin HandlersChain resolution failure).
        let src = "type Engine struct {\n\tpool int\n}\n";
        let tree = parse(src, Lang::Go);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "type_declaration");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("Engine".to_string())
        );
    }

    #[test]
    fn extract_definition_name_go_type_declaration_slice_alias() {
        let src = "type HandlersChain []HandlerFunc\n";
        let tree = parse(src, Lang::Go);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "type_declaration");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("HandlersChain".to_string())
        );
    }

    #[test]
    fn extract_definition_name_go_const_declaration() {
        let src = "const MaxRetries = 3\n";
        let tree = parse(src, Lang::Go);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "const_declaration");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("MaxRetries".to_string())
        );
    }

    #[test]
    fn extract_definition_name_go_var_declaration() {
        let src = "var GlobalVar = 1\n";
        let tree = parse(src, Lang::Go);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "var_declaration");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("GlobalVar".to_string())
        );
    }

    #[test]
    fn extract_definition_name_go_grouped_const_and_var_take_first_spec() {
        let src = "const (\n\tA = 1\n\tB = 2\n)\nvar (\n\tC = 3\n\tD = 4\n)\n";
        let tree = parse(src, Lang::Go);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "const_declaration");
        assert_eq!(extract_definition_name(node, &lines), Some("A".to_string()));
        let node = find_by_kind(tree.root_node(), "var_declaration");
        assert_eq!(extract_definition_name(node, &lines), Some("C".to_string()));
    }

    #[test]
    fn extract_definition_name_scala_var_declaration_uses_name_field() {
        let src = "trait Counter {\n  var count: Int\n}\n";
        let tree = parse(src, Lang::Scala);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "var_declaration");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("count".to_string())
        );
    }

    #[test]
    fn extract_definition_name_unwraps_export_statement() {
        // export_statement has no "name"/"identifier"/"declarator" field of its
        // own — extract_definition_name must recurse into the wrapped
        // function_declaration to find the name (the node.kind() == "export_statement"
        // branch).
        let src = "export function handler() {}\n";
        let tree = parse(src, Lang::TypeScript);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "export_statement");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("handler".to_string())
        );
    }

    #[test]
    fn extract_definition_name_walks_lexical_declaration_declarator() {
        // lexical_declaration stores its identifier inside a child
        // variable_declarator, not as a direct field on the declaration node —
        // exercises the dedicated child-walk branch.
        let src = "const total = 42;\n";
        let tree = parse(src, Lang::TypeScript);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "lexical_declaration");
        assert_eq!(
            extract_definition_name(node, &lines),
            Some("total".to_string())
        );
    }

    #[test]
    fn extract_definition_name_unwraps_owned_c_declarators() {
        for (language, source) in [
            (Lang::C, "int language_marker(void) { return 1; }\n"),
            (Lang::C, "int (*language_marker(void))(int) { return 0; }\n"),
            (Lang::Cpp, "int Widget::language_marker() { return 1; }\n"),
            (
                Lang::Cpp,
                "int (*language_marker(void))(int) { return 0; }\n",
            ),
        ] {
            let tree = parse(source, language);
            let lines: Vec<&str> = source.lines().collect();
            let node = find_by_kind(tree.root_node(), "function_definition");
            assert_eq!(
                extract_definition_name(node, &lines),
                Some("language_marker".to_string())
            );
        }
    }

    #[test]
    fn extract_definition_name_returns_none_when_no_name_field_present() {
        // impl_item has no "name"/"identifier"/"declarator" field and isn't
        // handled by any of the special-cased branches — must fall through to
        // None rather than panic or return an empty string.
        let src = "impl Widget {}\n";
        let tree = parse(src, Lang::Rust);
        let lines: Vec<&str> = src.lines().collect();
        let node = find_by_kind(tree.root_node(), "impl_item");
        assert_eq!(extract_definition_name(node, &lines), None);
    }

    fn rust_language() -> tree_sitter::Language {
        tree_sitter_rust::LANGUAGE.into()
    }

    fn assert_same_tree(actual: tree_sitter::Node<'_>, expected: tree_sitter::Node<'_>) {
        assert_eq!(actual.kind(), expected.kind());
        assert_eq!(actual.range(), expected.range());
        assert_eq!(actual.has_error(), expected.has_error());
        assert_eq!(actual.child_count(), expected.child_count());
        let (mut a, mut e) = (actual.walk(), expected.walk());
        for (a, e) in actual.children(&mut a).zip(expected.children(&mut e)) {
            assert_same_tree(a, e);
        }
    }

    fn document(source: &str) -> ParsedDocument {
        parse_document(source, &rust_language()).unwrap()
    }

    fn tree(document: &ParsedDocument) -> &tree_sitter::Tree {
        &document.root().get_doc().tree
    }

    #[test]
    fn incremental_reparse_reuses_unchanged_sibling_nodes() {
        let before = "fn a() { 1 }\nfn b() { 2 }\nfn c() { 3 }\n";
        let after = "fn a() { 1 }\nfn b() { 22 + 2 }\nfn c() { 3 }\n";
        let old = document(before);
        let new = document_after_edit(before, after, &old).unwrap();
        let (old_root, new_root) = (tree(&old).root_node(), tree(&new).root_node());
        assert_eq!(
            old_root.named_child(0).unwrap().id(),
            new_root.named_child(0).unwrap().id()
        );
        assert_eq!(
            old_root.named_child(2).unwrap().id(),
            new_root.named_child(2).unwrap().id()
        );
        assert_ne!(
            old_root.named_child(1).unwrap().id(),
            new_root.named_child(1).unwrap().id()
        );
    }

    #[test]
    fn incremental_reparse_matches_fresh_when_multibyte_chars_share_continuation_byte() {
        for (before, after) in [
            (
                "fn main() { Some(\"\u{e9}\"); }",
                "fn main() { Some(\"\u{a9}\"); }",
            ),
            (
                "fn main() { Some(\"\u{a9}\"); }",
                "fn main() { Some(\"\u{e9}\"); }",
            ),
            ("let s = \"cafe\";", "let s = \"caf\u{e9}\";"),
        ] {
            let incremental = document_after_edit(before, after, &document(before)).unwrap();
            assert_eq!(incremental.root().get_doc().src, after);
            let fresh = document(after);
            assert_same_tree(tree(&incremental).root_node(), tree(&fresh).root_node());
        }
    }
}
