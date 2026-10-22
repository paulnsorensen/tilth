//! Parse a command with `tree-sitter-bash` and check every simple command in it.

use super::hints::{Rule, DEVICE_PATH, INPUT_REDIRECT, OUTPUT_REDIRECT, PARSE_ERROR, UNRESOLVED};
use super::rules::{self, Arg, Cmd, Outcome};
use tree_sitter::{Node, Parser};

/// Nesting limit for `bash -c` and `eval` strings.
const MAX_DEPTH: usize = 8;

/// The first rule that denies `source`, or `None` when the whole command is allowed.
pub(super) fn check(source: &str) -> Option<Rule> {
    Walker { depth: 0 }.source(source)
}

#[derive(Clone, Copy, Default)]
struct Ctx {
    piped: bool,
    here: bool,
}

struct Walker {
    depth: usize,
}

impl Walker {
    /// Parses shell source and returns the first denial, including parse and depth failures.
    fn source(&mut self, src: &str) -> Option<Rule> {
        if self.depth > MAX_DEPTH {
            return Some(UNRESOLVED);
        }
        let mut parser = Parser::new();
        if parser
            .set_language(&tree_sitter_bash::LANGUAGE.into())
            .is_err()
        {
            return Some(PARSE_ERROR);
        }
        let Some(tree) = parser.parse(src, None) else {
            return Some(PARSE_ERROR);
        };
        let root = tree.root_node();
        if root.has_error() {
            return Some(PARSE_ERROR);
        }
        self.visit(root, src.as_bytes(), Ctx::default())
    }

    /// Checks a syntax node while carrying pipeline and heredoc context.
    fn visit(&mut self, node: Node, src: &[u8], ctx: Ctx) -> Option<Rule> {
        match node.kind() {
            "pipeline" => {
                let mut cursor = node.walk();
                for (i, child) in node.named_children(&mut cursor).enumerate() {
                    if let Some(rule) = self.visit(
                        child,
                        src,
                        Ctx {
                            piped: i > 0,
                            here: false,
                        },
                    ) {
                        return Some(rule);
                    }
                }
                None
            }
            "redirected_statement" => {
                let mut cursor = node.walk();
                let here = ctx.here || node.named_children(&mut cursor).any(|c| is_here(c.kind()));
                let body = node.child_by_field_name("body");
                let extra: Vec<Arg> = node
                    .named_children(&mut cursor)
                    .flat_map(|c| redirect_words(c, src))
                    .collect();
                if let Some(b) = body {
                    if b.kind() == "command" {
                        if let Some(rule) = self.command(
                            b,
                            src,
                            Ctx {
                                piped: ctx.piped,
                                here,
                            },
                            extra,
                        ) {
                            return Some(rule);
                        }
                        return self.redirects(node, src);
                    }
                }
                if !extra.is_empty() {
                    return Some(UNRESOLVED);
                }
                for child in node.named_children(&mut cursor) {
                    let inner = if Some(child.id()) == body.map(|b| b.id()) {
                        Ctx {
                            piped: ctx.piped,
                            here,
                        }
                    } else {
                        Ctx::default()
                    };
                    if let Some(rule) = self.visit(child, src, inner) {
                        return Some(rule);
                    }
                }
                None
            }
            "command" => self.command(node, src, ctx, Vec::new()),
            "file_redirect" => {
                if let Some(rule) = redirect(node, src) {
                    return Some(rule);
                }
                self.children(node, src)
            }
            _ => self.children(node, src),
        }
    }

    /// Checks named children with fresh context and returns the first denial.
    fn children(&mut self, node: Node, src: &[u8]) -> Option<Rule> {
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            if let Some(rule) = self.visit(child, src, Ctx::default()) {
                return Some(rule);
            }
        }
        None
    }

    /// Walk the redirects of a redirected statement whose body command is already checked.
    fn redirects(&mut self, node: Node, src: &[u8]) -> Option<Rule> {
        let mut cursor = node.walk();
        let body = node.child_by_field_name("body").map(|b| b.id());
        for child in node.named_children(&mut cursor) {
            if Some(child.id()) != body {
                if let Some(rule) = self.visit(child, src, Ctx::default()) {
                    return Some(rule);
                }
            }
        }
        None
    }

    /// Checks a simple command and reparses any shell source it runs.
    fn command(&mut self, node: Node, src: &[u8], ctx: Ctx, extra: Vec<Arg>) -> Option<Rule> {
        let mut cursor = node.walk();
        let mut words: Vec<Arg> = Vec::new();
        let mut here = ctx.here;
        for child in node.named_children(&mut cursor) {
            match child.kind() {
                "command_name" => words.insert(0, arg_of(child, src)),
                "variable_assignment" | "comment" => {}
                "file_redirect" => words.extend(redirect_words(child, src)),
                kind if is_here(kind) => {
                    here = true;
                    words.extend(redirect_words(child, src));
                }
                _ => words.push(arg_of(child, src)),
            }
        }
        words.extend(extra);
        if self.depth > 0 && words.first().is_some_and(|w| !w.literal) {
            return Some(UNRESOLVED);
        }
        let cmd = Cmd {
            words: &words,
            piped: ctx.piped,
            here,
        };
        match rules::check(&cmd) {
            Outcome::Allow => {}
            Outcome::Deny(rule) => return Some(rule),
            Outcome::Reparse(text) => {
                self.depth += 1;
                let found = self.source(&text);
                self.depth -= 1;
                if found.is_some() {
                    return found;
                }
            }
        }
        self.children(node, src)
    }
}

/// Words a redirect carries beyond its target: the grammar repeats `destination` in a file
/// redirect, and a heredoc marker is followed by arguments and further redirects. The shell
/// passes all of them to the command. Heredoc parts and nested commands are not operands.
fn redirect_words(node: Node, src: &[u8]) -> Vec<Arg> {
    let mut cursor = node.walk();
    match node.kind() {
        "file_redirect" => node
            .children_by_field_name("destination", &mut cursor)
            .skip(1)
            .map(|d| arg_of(d, src))
            .collect(),
        "heredoc_redirect" => node
            .named_children(&mut cursor)
            .flat_map(|child| match child.kind() {
                "file_redirect" | "heredoc_redirect" => redirect_words(child, src),
                kind if is_structure(kind) => Vec::new(),
                _ => vec![arg_of(child, src)],
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// Nodes under a heredoc redirect that are not words: the marker, the body, and nested commands.
fn is_structure(kind: &str) -> bool {
    matches!(
        kind,
        "heredoc_start"
            | "heredoc_body"
            | "heredoc_end"
            | "herestring_redirect"
            | "comment"
            | "variable_assignment"
    ) || kind.ends_with("_statement")
        || matches!(
            kind,
            "command" | "pipeline" | "list" | "subshell" | "compound_statement"
        )
}

/// Recognizes heredoc and here-string redirect nodes.
fn is_here(kind: &str) -> bool {
    matches!(kind, "heredoc_redirect" | "herestring_redirect")
}

/// Deny a redirect that reads a path or writes a file.
fn redirect(node: Node, src: &[u8]) -> Option<Rule> {
    let mut cursor = node.walk();
    let op = node
        .children(&mut cursor)
        .find(|c| !c.is_named())
        .and_then(|c| c.utf8_text(src).ok())
        .unwrap_or("");
    let dest = node.child_by_field_name("destination")?;
    if dest.kind() == "process_substitution" {
        return None;
    }
    let (text, literal) = literal(dest, src);
    if rules::is_device(&text) {
        return Some(DEVICE_PATH);
    }
    let fd_dup = literal && (text == "-" || text.chars().all(|c| c.is_ascii_digit()));
    let rule = match op {
        "<" | "<&" => INPUT_REDIRECT,
        _ => OUTPUT_REDIRECT,
    };
    let null = literal && text == "/dev/null";
    if null || (fd_dup && op.ends_with('&')) {
        None
    } else {
        Some(rule)
    }
}

/// Builds an argument with its unquoted text, literal status, and opacity.
fn arg_of(node: Node, src: &[u8]) -> Arg {
    let (text, literal) = literal(node, src);
    Arg {
        text,
        literal,
        opaque: opaque(node),
    }
}

/// Classifies expansions and substitutions as opaque while allowing word and string nodes.
fn opaque(node: Node) -> bool {
    match node.kind() {
        "command_name" => node.named_child(0).is_none_or(opaque),
        "word" | "number" | "string" | "raw_string" | "ansi_c_string" => false,
        "concatenation" => {
            let mut cursor = node.walk();
            let any = node.named_children(&mut cursor).any(|c| opaque(c));
            any
        }
        _ => true,
    }
}

/// The text of a word with quotes removed, and whether it holds no expansion.
fn literal(node: Node, src: &[u8]) -> (String, bool) {
    let raw = node.utf8_text(src).unwrap_or("");
    let unquote = |lead: usize, trail: usize| {
        raw.get(lead..raw.len().saturating_sub(trail))
            .unwrap_or(raw)
            .to_string()
    };
    match node.kind() {
        "command_name" => node
            .named_child(0)
            .map_or((raw.to_string(), false), |c| literal(c, src)),
        "word" | "number" => (unescape(raw), true),
        "string" => {
            let mut cursor = node.walk();
            let plain = node
                .named_children(&mut cursor)
                .all(|c| c.kind() == "string_content");
            (unquote(1, 1), plain)
        }
        "raw_string" => (unquote(1, 1), true),
        "ansi_c_string" => (unquote(2, 1), true),
        "concatenation" => {
            let mut cursor = node.walk();
            let mut text = String::new();
            let mut all = true;
            for child in node.named_children(&mut cursor) {
                let (t, l) = literal(child, src);
                text.push_str(&t);
                all &= l;
            }
            (text, all)
        }
        _ => (raw.to_string(), false),
    }
}

/// Removes backslash escapes from a bare word without evaluating shell expansions.
fn unescape(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    let mut chars = raw.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            if let Some(next) = chars.next() {
                out.push(next);
            }
        } else {
            out.push(c);
        }
    }
    out
}
