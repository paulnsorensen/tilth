//! Resolve a [`BlockAnchor`] to a concrete line span, wiring to tilth's
//! tree-sitter outline. The `#symbol` variant resolves the deepest
//! callable/type/container entry from one parsed tree; a line anchor resolves
//! to the outline block that begins on that line, else the innermost outline
//! block containing it.

#![allow(dead_code)]

use std::path::Path;

use super::parser::BlockAnchor;
use crate::types::{FileType, OutlineEntry};

/// A resolved 1-based inclusive line span.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlockSpan {
    pub start: u32,
    pub end: u32,
}

/// Resolve `anchor` against `text` (parsed as the language inferred from
/// `path`). Returns `None` for an unknown language, an out-of-range or blank
/// line, or a symbol/line that resolves to no block.
pub fn resolve_block(path: &Path, text: &str, anchor: &BlockAnchor) -> Option<BlockSpan> {
    outline_for(path, text).and_then(|entries| resolve_block_in(&entries, anchor))
}

/// Deep outline entries for `text` under the language inferred from `path`, or
/// `None` for a non-code file. Parsing the whole file is the expensive step, so
/// callers resolving several anchors should compute this once and reuse it via
/// [`resolve_block_in`].
pub fn outline_for(path: &Path, text: &str) -> Option<Vec<OutlineEntry>> {
    let FileType::Code(lang) = crate::lang::detect_file_type(path) else {
        return None;
    };
    Some(crate::lang::outline::get_deep_outline_tree(text, lang))
}

/// Resolve `anchor` against pre-computed outline `entries`.
pub fn resolve_block_in(entries: &[OutlineEntry], anchor: &BlockAnchor) -> Option<BlockSpan> {
    match anchor {
        BlockAnchor::Symbol(name) => {
            find_block_entry_by_name(entries, name).map(|(s, e)| BlockSpan { start: s, end: e })
        }
        BlockAnchor::Line(line) => {
            resolve_line(entries, *line).map(|(s, e)| BlockSpan { start: s, end: e })
        }
    }
}

fn find_block_entry_by_name(entries: &[OutlineEntry], name: &str) -> Option<(u32, u32)> {
    let mut pending: Vec<(&OutlineEntry, usize)> =
        entries.iter().rev().map(|entry| (entry, 0)).collect();
    let mut best = None;
    while let Some((entry, depth)) = pending.pop() {
        if !crate::lang::outline::is_path_line_entry_kind(entry.kind) {
            continue;
        }
        if entry.name == name && best.is_none_or(|(best_depth, _)| depth > best_depth) {
            best = Some((depth, (entry.span_start_line, entry.end_line)));
        }
        pending.extend(entry.children.iter().rev().map(|child| (child, depth + 1)));
    }
    best.map(|(_, span)| span)
}

/// Resolve a line anchor: prefer a block that *begins* on `line` (oh-my-pi's
/// "node begins here" semantics); otherwise the innermost block containing it.
fn resolve_line(entries: &[OutlineEntry], line: u32) -> Option<(u32, u32)> {
    if let Some(hit) = begins_on(entries, line) {
        return Some(hit);
    }
    innermost_containing(entries, line)
}

fn begins_on(entries: &[OutlineEntry], line: u32) -> Option<(u32, u32)> {
    let mut pending: Vec<&OutlineEntry> = entries.iter().rev().collect();
    let mut best = None;
    while let Some(entry) = pending.pop() {
        if !crate::lang::outline::is_path_line_entry_kind(entry.kind) {
            continue;
        }
        if (entry.start_line == line || entry.span_start_line == line)
            && best.is_none_or(|(start, end)| entry.end_line - entry.span_start_line < end - start)
        {
            best = Some((entry.span_start_line, entry.end_line));
        }
        pending.extend(entry.children.iter().rev());
    }
    best
}

fn innermost_containing(entries: &[OutlineEntry], line: u32) -> Option<(u32, u32)> {
    let mut pending: Vec<(&OutlineEntry, usize)> =
        entries.iter().rev().map(|entry| (entry, 0)).collect();
    let mut best = None;
    while let Some((entry, depth)) = pending.pop() {
        if !crate::lang::outline::is_path_line_entry_kind(entry.kind) {
            continue;
        }
        if line >= entry.span_start_line && line <= entry.end_line {
            if best.is_none_or(|(best_depth, _)| depth > best_depth) {
                best = Some((depth, (entry.span_start_line, entry.end_line)));
            }
            pending.extend(entry.children.iter().rev().map(|child| (child, depth + 1)));
        }
    }
    best.map(|(_, span)| span)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::outline::get_deep_outline_entries;
    use std::path::PathBuf;

    fn rs_path() -> PathBuf {
        // A `.rs` extension so detect_file_type picks the Rust grammar; the
        // path need not exist — resolution runs against `text`.
        PathBuf::from("resolve_block_fixture.rs")
    }

    const SRC: &str = "\
fn alpha() {
    let a = 1;
    a + 1
}

fn beta() {
    let b = 2;
    b + 2
}
";

    #[test]
    fn symbol_anchor_matches_read_selector_span() {
        let path = rs_path();
        let span =
            resolve_block(&path, SRC, &BlockAnchor::Symbol("beta".into())).expect("beta resolves");
        // The `#symbol` read selector resolves via the same outline entry, so
        // resolve_block must produce the identical (start,end).
        let entries = get_deep_outline_entries(SRC, crate::types::Lang::Rust);
        let expected = find_block_entry_by_name(&entries, "beta").expect("beta in outline");
        assert_eq!((span.start, span.end), expected);
        // And the span actually covers beta's opener line (line 6).
        assert_eq!(span.start, 6);
    }

    #[test]
    fn line_anchor_on_opener_resolves_enclosing_block() {
        let path = rs_path();
        // Line 1 is `fn alpha() {` — the block opener.
        let span =
            resolve_block(&path, SRC, &BlockAnchor::Line(1)).expect("line 1 resolves to a block");
        assert_eq!(span.start, 1);
        assert_eq!(span.end, 4, "alpha spans lines 1-4");
    }

    #[test]
    fn line_anchor_inside_body_resolves_enclosing_block() {
        let path = rs_path();
        // Line 7 is `let b = 2;` inside beta (lines 6-9).
        let span = resolve_block(&path, SRC, &BlockAnchor::Line(7)).expect("line 7 resolves");
        assert_eq!((span.start, span.end), (6, 9));
    }

    #[test]
    fn symbol_anchor_prefers_deepest_duplicate_in_sibling_subtrees() {
        let path = rs_path();
        let source = "\
mod left {
    fn duplicate() {}
}

mod right {
    mod nested {
        fn duplicate() {
            let value = 1;
        }
    }
}
";

        let span = resolve_block(&path, source, &BlockAnchor::Symbol("duplicate".into()))
            .expect("duplicate symbol resolves");
        assert_eq!((span.start, span.end), (7, 9));
    }

    #[test]
    fn unknown_symbol_yields_none() {
        let path = rs_path();
        assert!(resolve_block(&path, SRC, &BlockAnchor::Symbol("nonexistent".into())).is_none());
    }

    #[test]
    fn non_code_file_yields_none() {
        let path = PathBuf::from("data.bin.unknownext");
        assert!(resolve_block(&path, SRC, &BlockAnchor::Line(1)).is_none());
    }

    #[test]
    fn rust_attribute_line_resolves_semantic_block_start() {
        let path = rs_path();
        let source = "#[inline]\nfn alpha() {\n    let value = 1;\n}\n";

        let line_span = resolve_block(&path, source, &BlockAnchor::Line(1))
            .expect("attribute line should resolve to alpha");
        assert_eq!((line_span.start, line_span.end), (1, 4));

        let symbol_span = resolve_block(&path, source, &BlockAnchor::Symbol("alpha".into()))
            .expect("alpha symbol should resolve");
        assert_eq!((symbol_span.start, symbol_span.end), (1, 4));
    }
    #[test]
    fn nested_attributed_method_uses_deep_semantic_span() {
        let path = rs_path();
        let source = "\
mod a {
    mod b {
        #[inline]
        fn method() {
            let value = 1;
        }
    }
}
";

        let line_span = resolve_block(&path, source, &BlockAnchor::Line(3))
            .expect("attribute line should resolve to nested method");
        assert_eq!((line_span.start, line_span.end), (3, 6));

        let symbol_span = resolve_block(&path, source, &BlockAnchor::Symbol("method".into()))
            .expect("nested method symbol should resolve");
        assert_eq!((symbol_span.start, symbol_span.end), (3, 6));
    }

    #[test]
    fn deeply_nested_symbol_resolution_uses_iterative_hierarchy() {
        let path = rs_path();
        let depth = 512;
        let mut source = String::new();
        for _ in 0..depth {
            source.push_str("mod m {\n");
        }
        source.push_str("fn target() {\n    let value = 1;\n}\n");
        for _ in 0..depth {
            source.push_str("}\n");
        }

        let symbol_span = resolve_block(&path, &source, &BlockAnchor::Symbol("target".into()))
            .expect("deeply nested symbol resolves");
        assert_eq!(
            (symbol_span.start, symbol_span.end),
            (depth as u32 + 1, depth as u32 + 3)
        );

        let line_span = resolve_block(&path, &source, &BlockAnchor::Line(depth as u32 + 2))
            .expect("deeply nested body line resolves");
        assert_eq!(
            (line_span.start, line_span.end),
            (depth as u32 + 1, depth as u32 + 3)
        );
    }
}
