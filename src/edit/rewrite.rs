//! Structural rewrite: resolve an ast-grep pattern to byte-span replacements.
//!
//! Nested matches follow ast-grep's non-reentrant `replace_all`: once a node
//! matches, matches inside it are skipped, so the outermost match wins.

use std::path::Path;

use ast_grep_core::replacer::Replacer;

use super::apply::ApplyError;
use crate::types;

const MAX_REWRITE_OUTPUT: usize = 16 * 1024 * 1024;

pub(super) fn rewrite_match_spans(
    path: &Path,
    text: &str,
    pattern: &str,
    count: Option<usize>,
) -> Result<Vec<(usize, usize)>, ApplyError> {
    resolve_rewrite(path, text, pattern, "", count, false).map(|spans| {
        spans
            .into_iter()
            .map(|(start, end, _)| (start, end))
            .collect()
    })
}

/// Resolve outermost, non-overlapping matches before rendering replacements.
pub(super) fn rewrite_spans(
    path: &Path,
    text: &str,
    pattern: &str,
    rewrite: &str,
    count: Option<usize>,
) -> Result<Vec<(usize, usize, String)>, ApplyError> {
    resolve_rewrite(path, text, pattern, rewrite, count, true)
}

fn resolve_rewrite(
    path: &Path,
    text: &str,
    pattern: &str,
    rewrite: &str,
    count: Option<usize>,
    render: bool,
) -> Result<Vec<(usize, usize, String)>, ApplyError> {
    let unsupported = || ApplyError::RewriteLanguage {
        path: path.display().to_string(),
    };
    let types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
        return Err(unsupported());
    };
    let spec = crate::lang::spec::spec(lang);
    let (Some(support), Some(grammar)) = (spec.policy.structural, spec.grammar) else {
        return Err(unsupported());
    };
    let compiled = crate::search::structural::compile_pattern(lang, support, pattern)
        .map_err(|message| ApplyError::RewritePattern { message })?;
    let grammar = tree_sitter::Language::new(grammar);
    let document =
        crate::lang::treesitter::parse_document(text, &grammar).ok_or_else(unsupported)?;
    let mut last_end = 0;
    let matches: Vec<_> = document
        .root()
        .find_all(&compiled)
        .filter(|matched| {
            let range = rewrite.get_replaced_range(matched, &compiled);
            if range.start < last_end {
                false
            } else {
                last_end = range.end;
                true
            }
        })
        .collect();
    if matches.is_empty() {
        return Err(ApplyError::RewriteUnmatched {
            pattern: pattern.to_string(),
        });
    }
    if let Some(expected) = count {
        if matches.len() != expected {
            return Err(ApplyError::RewriteCountMismatch {
                pattern: pattern.to_string(),
                expected,
                found: matches.len(),
            });
        }
    }
    if !render {
        return Ok(matches
            .iter()
            .map(|matched| {
                let range = rewrite.get_replaced_range(matched, &compiled);
                (range.start, range.end, String::new())
            })
            .collect());
    }
    if !rewrite.contains('$') && rewrite.len().saturating_mul(matches.len()) > MAX_REWRITE_OUTPUT {
        return Err(ApplyError::RewriteOutputTooLarge);
    }
    let mut total = 0usize;
    let mut spans = Vec::with_capacity(matches.len());
    for matched in matches {
        let edit = matched.make_edit(&compiled, &rewrite);
        total = total.saturating_add(edit.inserted_text.len());
        if total > MAX_REWRITE_OUTPUT {
            return Err(ApplyError::RewriteOutputTooLarge);
        }
        spans.push((
            edit.position,
            edit.position + edit.deleted_length,
            String::from_utf8_lossy(&edit.inserted_text).into_owned(),
        ));
    }
    Ok(spans)
}

#[cfg(test)]
mod tests {
    use super::super::apply::apply_ops;
    use super::super::parser::Op;
    use super::*;

    fn rw(pattern: &str, rewrite: &str, count: Option<usize>) -> Op {
        Op::Rewrite {
            pattern: pattern.into(),
            rewrite: rewrite.into(),
            count,
        }
    }

    fn run(file: &str, text: &str, ops: &[Op]) -> Result<String, ApplyError> {
        apply_ops(Path::new(file), text, ops).map(|r| r.text)
    }

    const GO: &str =
        "package main\n\nfunc a() {\n\tw.Render(p)\n\ts.tpl.Render(q.Z())\n\tRender(r)\n}\n";

    #[test]
    fn go_receiver_call_gains_context_argument_for_every_receiver() {
        let op = rw("$R.Render($W)", "$R.Render(context.Background(), $W)", None);
        let out = run("a.go", GO, &[op]).expect("rewrite");
        assert_eq!(
            out,
            "package main\n\nfunc a() {\n\tw.Render(context.Background(), p)\n\ts.tpl.Render(context.Background(), q.Z())\n\tRender(r)\n}\n"
        );
    }

    #[test]
    fn count_match_succeeds_and_mismatch_reports_both_numbers() {
        let op = |n| rw("$R.Render($W)", "$R.Draw($W)", Some(n));
        assert!(run("a.go", GO, &[op(2)]).is_ok());
        let err = run("a.go", GO, &[op(12)]).unwrap_err();
        assert_eq!(
            err.to_string(),
            "rewrite expected 12 matches of pattern \"$R.Render($W)\" but found 2; no change written"
        );
        assert!(err.is_text_match_failure());
    }

    #[test]
    fn zero_matches_is_an_error() {
        let err = run("a.go", GO, &[rw("nothing($A)", "x($A)", None)]).unwrap_err();
        assert_eq!(
            err.to_string(),
            "rewrite pattern \"nothing($A)\" matched nothing"
        );
        assert!(err.is_text_match_failure());
    }

    #[test]
    fn invalid_pattern_surfaces_the_pattern_error() {
        let err = run("a.go", GO, &[rw("func (", "x", None)]).unwrap_err();
        assert!(
            matches!(&err, ApplyError::RewritePattern { message } if !message.is_empty()),
            "{err:?}"
        );
        assert!(err.to_string().contains("rewrite pattern is invalid"));
        assert!(err.is_text_match_failure());
    }

    #[test]
    fn unsupported_language_names_the_file() {
        for file in ["notes.txt", "Makefile", "Dockerfile"] {
            let err = run(file, "f(1)\n", &[rw("f($A)", "g($A)", None)]).unwrap_err();
            assert!(
                matches!(&err, ApplyError::RewriteLanguage { path } if path == file),
                "{file}: {err:?}"
            );
            assert!(err.to_string().contains(file), "{err}");
        }
    }

    #[test]
    fn multi_line_match_is_replaced_whole() {
        let text = "func a() {\n\trun(func() {\n\t\tx()\n\t})\n\ty()\n}\n";
        let out = run("a.go", text, &[rw("run($F)", "runCtx(ctx, $F)", None)]).expect("rewrite");
        assert_eq!(
            out,
            "func a() {\n\trunCtx(ctx, func() {\n\t\tx()\n\t})\n\ty()\n}\n"
        );
    }

    #[test]
    fn multi_metavariable_captures_all_arguments() {
        let text = "fn m() {\n    log(1, 2, 3);\n    log();\n}\n";
        let out = run(
            "a.rs",
            text,
            &[rw("log($$$ARGS)", "trace(0, $$$ARGS)", None)],
        )
        .expect("rewrite");
        assert_eq!(
            out,
            "fn m() {\n    trace(0, 1, 2, 3);\n    trace(0, );\n}\n"
        );
    }

    #[test]
    fn rust_and_typescript_examples() {
        let rust = "fn m(a: Option<u8>) {\n    let x = a.unwrap();\n    let y = b().unwrap();\n}\n";
        let out = run(
            "a.rs",
            rust,
            &[rw("$X.unwrap()", "$X.expect(\"set\")", None)],
        )
        .unwrap();
        assert_eq!(
            out,
            "fn m(a: Option<u8>) {\n    let x = a.expect(\"set\");\n    let y = b().expect(\"set\");\n}\n"
        );
        let ts = "console.log(a);\nfoo(console.log(b));\n";
        let out = run(
            "a.ts",
            ts,
            &[rw("console.log($A)", "logger.info($A)", None)],
        )
        .unwrap();
        assert_eq!(out, "logger.info(a);\nfoo(logger.info(b));\n");
    }

    /// ast-grep replaces non-reentrantly: when a match contains another match,
    /// only the outer one is rewritten and the inner text is kept as captured.
    #[test]
    fn nested_matches_rewrite_only_the_outermost() {
        let text = "fn m() {\n    let a = Some(Some(1));\n}\n";
        let out = run("a.rs", text, &[rw("Some($A)", "Wrap($A)", None)]).unwrap();
        assert_eq!(out, "fn m() {\n    let a = Wrap(Some(1));\n}\n");
    }

    #[test]
    fn rewrite_overlapping_another_op_is_rejected() {
        let text = "fn m() {\n    f(1);\n    f(2);\n}\n";
        let ops = vec![
            rw("f($A)", "g($A)", None),
            Op::Swap {
                start: 3,
                end: 3,
                payload: vec!["    other();".into()],
            },
        ];
        let err = run("a.rs", text, &ops).unwrap_err();
        assert!(matches!(err, ApplyError::Overlap { .. }), "{err:?}");
    }

    #[test]
    fn same_line_matches_coalesce_with_a_text_swap() {
        let text = "fn m() {\n    f(1); f(2); tail();\n}\n";
        let ops = vec![
            rw("f($A)", "g($A)", None),
            Op::TextSwap {
                old: "tail".into(),
                new: "end".into(),
            },
        ];
        let out = run("a.rs", text, &ops).unwrap();
        assert_eq!(out, "fn m() {\n    g(1); g(2); end();\n}\n");
    }
    #[test]
    fn count_rejection_precedes_large_replacement_guard() {
        let text = "fn m() { f(1); f(2); }";
        let huge = "x".repeat(MAX_REWRITE_OUTPUT + 1);
        let err = rewrite_spans(Path::new("a.rs"), text, "f($A)", &huge, Some(3)).unwrap_err();
        assert!(matches!(
            err,
            ApplyError::RewriteCountMismatch { found: 2, .. }
        ));
    }

    #[test]
    fn aggregate_rewrite_output_is_bounded_before_rendering() {
        let text = "fn m() { f(1); f(2); }";
        let huge = "x".repeat(MAX_REWRITE_OUTPUT / 2 + 1);
        let err = rewrite_spans(Path::new("a.rs"), text, "f($A)", &huge, Some(2)).unwrap_err();
        assert!(matches!(err, ApplyError::RewriteOutputTooLarge));
        assert_eq!(
            rewrite_match_spans(Path::new("a.rs"), text, "f($A)", Some(2))
                .unwrap()
                .len(),
            2
        );
    }

    #[test]
    fn large_single_line_rewrite_under_limit_survives_indentation() {
        let text = format!("fn m() {{\n{}f(1);\n}}\n", " ".repeat(32));
        let replacement = "x".repeat(1024 * 1024);
        let spans = rewrite_spans(Path::new("a.rs"), &text, "f($A)", &replacement, Some(1))
            .expect("output below limit");
        assert_eq!(spans.len(), 1);
        assert_eq!(spans[0].2, replacement);
    }

    #[test]
    fn large_capture_under_limit_is_not_rejected_by_preflight() {
        let capture = format!("\"{}\"", "x".repeat(6 * 1024 * 1024));
        let text = format!("f({capture})");
        let spans = rewrite_spans(Path::new("a.rs"), &text, "f($A)", "g($A)", Some(1))
            .expect("rendered output below limit");
        assert_eq!(spans.len(), 1);
        assert_eq!(spans[0].2, format!("g({capture})"));
    }
}
