//! Parse a command with `tree-sitter-bash` and check every simple command in it.

use super::hints::{Rule, DEVICE_PATH, INPUT_REDIRECT, OUTPUT_REDIRECT, PARSE_ERROR, UNRESOLVED};
use super::rules::{self, Arg, Cmd, Feed, Outcome};
use super::Capture;
use std::cell::Cell;
use tree_sitter::{Node, Parser};

/// Nesting limit for `bash -c` and `eval` strings.
const MAX_DEPTH: usize = 8;

/// The first rule that denies `source`, or `None` when the whole command is allowed.
pub(super) fn check(source: &str, capture: Capture) -> Option<Rule> {
    Walker { depth: 0, capture }.source(source).err()
}

#[derive(Clone, Copy, Default)]
struct Ctx {
    piped: bool,
    here: bool,
    skip_fd0: bool,
    /// A trailing redirect connects the last command, not the first pipeline stage.
    tail_stdin: bool,
    /// Where the stdin of the node comes from.
    feed: Feed,
}

/// Whether authored content reaches the stdout of a walked node. Authored content is text from
/// a content generator (`echo`, `printf`, `cat`), from a filter with no real input, or from a
/// command that the rules cannot name. `--allow-capture` lets a file redirect pass only when
/// its content is not authored.
#[derive(Clone, Copy, Default)]
struct Authored {
    /// Some command in the node writes authored content to the stdout of the node.
    any: bool,
    /// The command that owns a redirect after the node writes authored content. The grammar
    /// puts the redirect of `a && b > f` on the whole list, but the shell gives it to `b`.
    last: bool,
}

impl Authored {
    /// A redirect with no command, where only the shell writes the file.
    const NO_COMMAND: Self = Self {
        any: true,
        last: true,
    };

    /// The feed that the node gives to the next pipeline stage.
    fn feed(self) -> Feed {
        if self.any {
            Feed::Authored
        } else {
            Feed::Real
        }
    }

    /// What writes through a redirect after the node.
    fn writer(self) -> Feed {
        if self.last {
            Feed::Authored
        } else {
            Feed::Real
        }
    }
}

/// The authored state of a node, or the rule that denies it.
type Walk = Result<Authored, Rule>;

struct Walker {
    depth: usize,
    capture: Capture,
}

impl Walker {
    /// Parses shell source and returns the first denial, including parse and depth failures.
    fn source(&mut self, src: &str) -> Walk {
        if self.depth > MAX_DEPTH {
            return Err(UNRESOLVED);
        }
        let mut parser = Parser::new();
        if parser
            .set_language(&tree_sitter_bash::LANGUAGE.into())
            .is_err()
        {
            return Err(PARSE_ERROR);
        }
        let Some(tree) = parser.parse(src, None) else {
            return Err(PARSE_ERROR);
        };
        let root = tree.root_node();
        if root.has_error() {
            return Err(PARSE_ERROR);
        }
        self.visit(root, src.as_bytes(), Ctx::default())
    }

    /// Checks a syntax node while carrying pipeline and heredoc context. The output of a
    /// substitution goes to a word, so it never counts as output of the statement.
    fn visit(&mut self, node: Node, src: &[u8], ctx: Ctx) -> Walk {
        match node.kind() {
            "pipeline" => self.pipeline(node, src, ctx),
            "list" | "negated_command" => {
                let mut out = Authored::default();
                let mut cursor = node.walk();
                let children: Vec<_> = node
                    .named_children(&mut cursor)
                    .filter(|child| child.kind() != "comment")
                    .collect();
                let count = children.len();
                for (i, child) in children.into_iter().enumerate() {
                    let child_ctx = if i + 1 == count { ctx } else { Ctx::default() };
                    let Authored { any, last } = self.visit(child, src, child_ctx)?;
                    out = Authored {
                        any: out.any || any,
                        last,
                    };
                }
                Ok(out)
            }
            "redirected_statement" => self.redirected(node, src, ctx),
            "command" => self.command(node, src, ctx, Vec::new()),
            "file_redirect" | "heredoc_redirect" => Ok(self
                .redirect_node(node, src, Feed::Unknown)?
                .unwrap_or_default()),
            "command_substitution" | "process_substitution" => {
                self.children(node, src)?;
                Ok(Authored::default())
            }
            _ => self.children(node, src),
        }
    }

    /// Checks the stages of a pipeline. The first stage reads what feeds the pipeline, and each
    /// later stage reads the stage before it. The grammar puts a heredoc on the whole pipeline,
    /// but the shell gives it to the last stage.
    fn pipeline(&mut self, node: Node, src: &[u8], ctx: Ctx) -> Walk {
        let mut out = Authored::default();
        let mut cursor = node.walk();
        let stages: Vec<_> = node
            .named_children(&mut cursor)
            .filter(|child| child.kind() != "comment")
            .collect();
        let last = stages.len().saturating_sub(1);
        for (i, child) in stages.into_iter().enumerate() {
            let feed = if i == last && ctx.tail_stdin {
                Feed::Connected
            } else if i == last && ctx.here {
                Feed::Authored
            } else if i == 0 {
                ctx.feed
            } else {
                out.feed()
            };
            let stage = Ctx {
                piped: ctx.piped || i > 0,
                here: false,
                skip_fd0: i == last && ctx.skip_fd0,
                tail_stdin: i == last && ctx.tail_stdin,
                feed,
            };
            out = self.visit(child, src, stage)?;
        }
        Ok(out)
    }

    /// Checks named children with fresh context and returns the first denial.
    fn children(&mut self, node: Node, src: &[u8]) -> Walk {
        let mut any = false;
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            any |= self.visit(child, src, Ctx::default())?.any;
        }
        Ok(Authored { any, last: any })
    }

    /// Checks a statement with redirects: the body first, then each redirect.
    fn redirected(&mut self, node: Node, src: &[u8], ctx: Ctx) -> Walk {
        let mut cursor = node.walk();
        let here = ctx.here || node.named_children(&mut cursor).any(|c| is_here(c.kind()));
        let body = node.child_by_field_name("body");
        let extra: Vec<Arg> = node
            .named_children(&mut cursor)
            .flat_map(|c| redirect_words(c, src))
            .collect();
        let connected = node
            .named_children(&mut cursor)
            .any(|child| permitted_stdin(child, src, self.capture));
        let own_fd0 = node.named_children(&mut cursor).any(|child| {
            permitted_stdin(child, src, self.capture)
                && explicit_fd0(child, src)
                && child.child_by_field_name("descriptor").is_none()
        });
        let simple = body.is_some_and(|b| b.kind() == "command");
        let inner = Ctx {
            here,
            skip_fd0: ctx.skip_fd0 || own_fd0,
            tail_stdin: ctx.tail_stdin || (connected && !simple),
            feed: if connected && simple {
                Feed::Connected
            } else {
                ctx.feed
            },
            ..ctx
        };
        let mut out = match body {
            Some(b) if b.kind() == "command" => self.command(b, src, inner, extra)?,
            _ if !extra.is_empty() => return Err(UNRESOLVED),
            Some(b) => self.visit(b, src, inner)?,
            None => Authored::NO_COMMAND,
        };
        for child in node.named_children(&mut cursor) {
            if Some(child.id()) != body.map(|b| b.id()) {
                if let Some(tail) = self.redirect_node(child, src, out.writer())? {
                    out = tail;
                }
            }
        }
        Ok(out)
    }

    /// Checks one redirect of a statement. `writer` is what writes through the redirect: with
    /// `--allow-capture`, an output redirect to a file passes when a real command writes.
    ///
    /// The grammar puts two things under the heredoc of `a <<EOF > f | b`: the file redirects
    /// of `a`, and the rest of the pipeline, which reads the output of `a`.
    fn redirect_node(
        &mut self,
        node: Node,
        src: &[u8],
        writer: Feed,
    ) -> Result<Option<Authored>, Rule> {
        let mut tail = None;
        match node.kind() {
            "file_redirect" => {
                let captured = self.capture == Capture::Allow && writer == Feed::Real;
                match redirect(node, src, self.capture) {
                    Some(OUTPUT_REDIRECT) if captured => {}
                    Some(rule) => return Err(rule),
                    None => {}
                }
                self.children(node, src)?;
            }
            "heredoc_redirect" => {
                let mut cursor = node.walk();
                for child in node.named_children(&mut cursor) {
                    match child.kind() {
                        "file_redirect" => {
                            self.redirect_node(child, src, writer)?;
                        }
                        "pipeline" => {
                            let fed = Ctx {
                                feed: writer,
                                ..Ctx::default()
                            };
                            tail = Some(self.visit(child, src, Ctx { piped: true, ..fed })?);
                        }
                        _ => {
                            self.visit(child, src, Ctx::default())?;
                        }
                    }
                }
            }
            _ => {
                self.visit(node, src, Ctx::default())?;
            }
        }
        Ok(tail)
    }

    /// Checks a simple command and reparses any shell source it runs. A command with no words
    /// holds only redirects and assignments, so no program writes its output.
    fn command(&mut self, node: Node, src: &[u8], mut ctx: Ctx, extra: Vec<Arg>) -> Walk {
        if ctx.tail_stdin {
            ctx.feed = Feed::Connected;
        }
        let mut cursor = node.walk();
        let mut words: Vec<Arg> = Vec::new();
        let mut here = ctx.here;
        let mut assigns: Vec<Arg> = Vec::new();
        for child in node.named_children(&mut cursor) {
            match child.kind() {
                "command_name" => words.insert(0, arg_of(child, src)),
                "variable_assignment" => assigns.push(assignment(child, src)),
                "comment" => {}
                "file_redirect" => words.extend(redirect_words(child, src)),
                kind if is_here(kind) => {
                    here = true;
                    words.extend(redirect_words(child, src));
                }
                _ => words.push(arg_of(child, src)),
            }
        }
        if ctx.skip_fd0 {
            words.pop();
        }
        words.extend(extra);
        if node
            .named_children(&mut cursor)
            .any(|child| permitted_stdin(child, src, self.capture))
        {
            ctx.feed = Feed::Connected;
        }
        if self.depth > 0 && words.first().is_some_and(|w| !w.literal) {
            return Err(UNRESOLVED);
        }
        let authored = Cell::new(words.is_empty());
        let cmd = Cmd {
            words: &words,
            assigns: &assigns,
            piped: ctx.piped,
            here,
            capture: self.capture,
            feed: ctx.feed,
            authored: &authored,
        };
        match rules::check(&cmd) {
            Outcome::Allow => {}
            Outcome::Deny(rule) => return Err(rule),
            Outcome::Reparse(text) => {
                self.depth += 1;
                let found = self.source(&text);
                self.depth -= 1;
                if found?.any {
                    authored.set(true);
                }
            }
        }
        let authored = authored.get();
        let mut out = Authored {
            any: authored,
            last: authored,
        };
        for child in node.named_children(&mut cursor) {
            match child.kind() {
                "file_redirect" | "heredoc_redirect" => {
                    if let Some(tail) = self.redirect_node(child, src, out.writer())? {
                        out = tail;
                    }
                }
                _ => {
                    self.visit(child, src, Ctx::default())?;
                }
            }
        }
        Ok(out)
    }
}

/// A leading `NAME=value` word of a command, with the value unquoted.
fn assignment(node: Node, src: &[u8]) -> Arg {
    let name = node
        .child_by_field_name("name")
        .and_then(|n| n.utf8_text(src).ok())
        .unwrap_or_default();
    let value = node.child_by_field_name("value").map(|v| arg_of(v, src));
    Arg {
        text: format!("{name}={}", value.as_ref().map_or("", |v| v.text.as_str())),
        literal: value.as_ref().is_none_or(|v| v.literal),
        opaque: false,
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

/// Return an explicit descriptor immediately before a redirect operator.
fn explicit_fd<'a>(node: Node, src: &'a [u8]) -> Option<&'a [u8]> {
    if let Some(fd) = node.child_by_field_name("descriptor") {
        return Some(&src[fd.start_byte()..fd.end_byte()]);
    }
    let previous = node.prev_named_sibling()?;
    let number = if previous.kind() == "command" {
        previous.named_child(
            previous
                .named_child_count()
                .checked_sub(1)?
                .try_into()
                .ok()?,
        )?
    } else {
        previous
    };
    (number.kind() == "number" && number.end_byte() == node.start_byte())
        .then_some(&src[number.start_byte()..number.end_byte()])
}

fn explicit_fd0(node: Node, src: &[u8]) -> bool {
    explicit_fd(node, src) == Some(b"0".as_slice())
}

/// Whether a permitted literal file connects descriptor zero without proving a real writer.
fn permitted_stdin(node: Node, src: &[u8], capture: Capture) -> bool {
    if node.kind() != "file_redirect" || capture != Capture::Allow {
        return false;
    }
    let mut cursor = node.walk();
    let Some(op) = node.children(&mut cursor).find(|child| !child.is_named()) else {
        return false;
    };
    if op.utf8_text(src).ok() != Some("<") {
        return false;
    }
    let fd = explicit_fd(node, src);
    if fd.is_some_and(|fd| fd != b"0") {
        return false;
    }
    node.child_by_field_name("destination").is_some_and(|dest| {
        let (text, literal) = literal(dest, src);
        rules::is_pseudo_file(&text, literal)
    })
}

/// Deny a redirect that reads a path or writes a file. With `--allow-capture`, output to a
/// stream device and input from a pseudo-file pass. The caller decides whether an
/// `OUTPUT_REDIRECT` result is a capture that passes.
fn redirect(node: Node, src: &[u8], capture: Capture) -> Option<Rule> {
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
    let input = matches!(op, "<" | "<&");
    let lifted = match capture {
        Capture::Deny => false,
        Capture::Allow if input => rules::is_pseudo_file(&text, literal),
        Capture::Allow => rules::is_stream_target(&text, literal),
    };
    if lifted {
        return None;
    }
    if rules::is_device(&text) {
        return Some(DEVICE_PATH);
    }
    let fd_dup = literal && (text == "-" || text.chars().all(|c| c.is_ascii_digit()));
    let rule = if input {
        INPUT_REDIRECT
    } else {
        OUTPUT_REDIRECT
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
        opaque: opaque(node, src),
    }
}

/// Classifies expansions and substitutions as opaque while allowing word and string nodes.
fn opaque(node: Node, src: &[u8]) -> bool {
    match node.kind() {
        "command_name" => node.named_child(0).is_none_or(|c| opaque(c, src)),
        "word" | "number" | "string" | "raw_string" => false,
        "ansi_c_string" => !literal(node, src).1,
        "concatenation" => {
            let mut cursor = node.walk();
            let any = node.named_children(&mut cursor).any(|c| opaque(c, src));
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
            (double_quoted_body(node, src), plain)
        }
        "raw_string" => (unquote(1, 1), true),
        "ansi_c_string" => match decode_ansi_c(&unquote(2, 1)) {
            Some(text) => (text, true),
            None => (unquote(2, 1), false),
        },
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

/// The body of a double-quoted string as the shell passes it: `\$`, `` \` ``, `\"`, `\\`, and
/// backslash-newline lose the backslash. Expansion nodes keep their source text.
fn double_quoted_body(node: Node, src: &[u8]) -> String {
    let text = |from: usize, to: usize| {
        src.get(from..to)
            .and_then(|b| std::str::from_utf8(b).ok())
            .unwrap_or("")
    };
    let end = node.end_byte().saturating_sub(1);
    let mut pos = node.start_byte() + 1;
    let mut out = String::new();
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "string_content" {
            continue;
        }
        out.push_str(&unescape_double(text(pos, child.start_byte())));
        out.push_str(text(child.start_byte(), child.end_byte()));
        pos = child.end_byte();
    }
    out.push_str(&unescape_double(text(pos, end)));
    out
}

fn unescape_double(body: &str) -> String {
    let mut out = String::with_capacity(body.len());
    let mut chars = body.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('\n') => {}
            Some(n @ ('$' | '`' | '"' | '\\')) => out.push(n),
            Some(n) => {
                out.push('\\');
                out.push(n);
            }
            None => out.push('\\'),
        }
    }
    out
}

/// Decode the body of an ANSI-C `$'...'` string. `None` when the result is not the text the
/// shell would pass: a bad escape, a NUL byte, or bytes that are not UTF-8.
fn decode_ansi_c(body: &str) -> Option<String> {
    let b = body.as_bytes();
    let digits = |from: usize, max: usize, radix: u32| {
        b[from..]
            .iter()
            .take(max)
            .take_while(|c| char::from(**c).is_digit(radix))
            .count()
    };
    let mut out: Vec<u8> = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'\\' {
            out.push(b[i]);
            i += 1;
            continue;
        }
        let c = *b.get(i + 1)?;
        i += 2;
        match c {
            b'a' => out.push(7),
            b'b' => out.push(8),
            b'e' | b'E' => out.push(27),
            b'f' => out.push(12),
            b'n' => out.push(10),
            b'r' => out.push(13),
            b't' => out.push(9),
            b'v' => out.push(11),
            b'\\' | b'\'' | b'"' | b'?' => out.push(c),
            b'0'..=b'7' => {
                let n = 1 + digits(i, 2, 8);
                let value =
                    u32::from_str_radix(std::str::from_utf8(&b[i - 1..i + n - 1]).ok()?, 8).ok()?;
                out.push((value & 0xff) as u8);
                i += n - 1;
            }
            b'x' => {
                let n = digits(i, 2, 16);
                if n == 0 {
                    out.extend_from_slice(b"\\x");
                } else {
                    let value =
                        u8::from_str_radix(std::str::from_utf8(&b[i..i + n]).ok()?, 16).ok()?;
                    out.push(value);
                    i += n;
                }
            }
            b'u' | b'U' => {
                let n = digits(i, if c == b'u' { 4 } else { 8 }, 16);
                if n == 0 {
                    return None;
                }
                let value =
                    u32::from_str_radix(std::str::from_utf8(&b[i..i + n]).ok()?, 16).ok()?;
                let ch = char::from_u32(value)?;
                out.extend_from_slice(ch.to_string().as_bytes());
                i += n;
            }
            b'c' => {
                let x = *b.get(i)?;
                i += 1;
                out.push(if x == b'?' { 127 } else { x & 0x1f });
            }
            other => {
                out.push(b'\\');
                out.push(other);
            }
        }
    }
    if out.contains(&0) {
        return None;
    }
    String::from_utf8(out).ok()
}
