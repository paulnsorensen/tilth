use crate::lang::treesitter::ParsedDocument;
use crate::types::Lang;

type DocNode<'r> = ast_grep_core::Node<
    'r,
    ast_grep_core::tree_sitter::StrDoc<crate::lang::treesitter::DocumentLanguage>,
>;

/// Extract test structure (describe/it/test) from a parsed file.
/// Returns a structured test outline with suite nesting, or None if
/// no test structure was found.
pub fn outline(content: &str, lang: Lang, max_lines: usize) -> Option<(String, bool)> {
    let language = crate::lang::outline::outline_language(lang)?;
    let document = crate::lang::treesitter::parse_document(content, &language)?;
    outline_from_document(max_lines, &document)
}

/// Each test call becomes one line, indented by the number of enclosing suites.
pub(crate) fn outline_from_document(
    max_lines: usize,
    document: &ParsedDocument,
) -> Option<(String, bool)> {
    let root = document.root();
    let mut calls = root.dfs().filter_map(|node| {
        let call = test_call(&node)?;
        let depth = node
            .ancestors()
            .filter(|ancestor| test_call(ancestor).is_some_and(|c| c.is_suite))
            .count();
        let line = node.start_pos().line() + 1;
        let label = if call.is_suite { "suite" } else { "test" };
        Some(format!(
            "{}[{line}] {label}: {}",
            "  ".repeat(depth),
            call.name
        ))
    });

    let entries: Vec<String> = calls.by_ref().take(max_lines).collect();
    if entries.is_empty() {
        return None;
    }
    let truncated = calls.next().is_some();
    Some((entries.join("\n"), truncated))
}

struct TestCall {
    name: String,
    is_suite: bool,
}

/// Match `describe("…", …)`, `it("…", …)`, and their aliases.
fn test_call(node: &DocNode<'_>) -> Option<TestCall> {
    if !matches!(&*node.kind(), "call_expression" | "expression_statement") {
        return None;
    }
    let func = node.children().find(|child| {
        matches!(
            &*child.kind(),
            "identifier" | "member_expression" | "call_expression"
        )
    })?;
    let func = first_line(&func);
    if !matches!(
        func.as_str(),
        "describe" | "it" | "test" | "context" | "specify"
    ) {
        return None;
    }

    let args = node.children().find(|child| child.kind() == "arguments")?;
    let title = args.children().find(|child| {
        matches!(
            &*child.kind(),
            "string" | "template_string" | "string_literal"
        )
    })?;
    let title = first_line(&title);
    let title = title.trim_matches('"').trim_matches('\'').trim_matches('`');

    Some(TestCall {
        is_suite: matches!(func.as_str(), "describe" | "context"),
        name: format!("{func}(\"{title}\")"),
    })
}

fn first_line(node: &DocNode<'_>) -> String {
    node.text().lines().next().unwrap_or_default().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nests_tests_under_suites_in_source_order() {
        let source = concat!(
            "describe('auth', () => {\n",
            "  it('logs in', () => {});\n",
            "  context('expired', () => {\n",
            "    test(`refreshes`, () => {});\n",
            "  });\n",
            "});\n",
            "it('top level', () => {});\n",
        );
        let (text, truncated) = outline(source, Lang::TypeScript, usize::MAX).unwrap();
        assert_eq!(
            text,
            concat!(
                "[1] suite: describe(\"auth\")\n",
                "  [2] test: it(\"logs in\")\n",
                "  [3] suite: context(\"expired\")\n",
                "    [4] test: test(\"refreshes\")\n",
                "[7] test: it(\"top level\")",
            )
        );
        assert!(!truncated);
    }

    #[test]
    fn reports_truncation_only_when_calls_remain() {
        let source = "it('a', () => {});\nit('b', () => {});\n";
        let (text, truncated) = outline(source, Lang::JavaScript, 1).unwrap();
        assert_eq!(text, "[1] test: it(\"a\")");
        assert!(truncated);
        let (_, truncated) = outline(source, Lang::JavaScript, 2).unwrap();
        assert!(!truncated);
    }

    #[test]
    fn ignores_files_without_test_calls() {
        assert!(outline("const x = run('a');\n", Lang::JavaScript, usize::MAX).is_none());
    }
}
