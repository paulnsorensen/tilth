//! The deny-list: one simple command in, an outcome out.

use super::hints::{
    Rule, AWK_INPLACE, DEVICE_PATH, GIT_APPLY, GIT_CAT_FILE, GIT_GREP, GIT_SHOW, JSON_TOOL, PERL_I,
    SED_I, SHELL_STDIN, UNRESOLVED,
};

/// One word of a simple command, with quotes removed.
pub(super) struct Arg {
    pub text: String,
    /// The text is exactly what the shell passes (no expansion inside).
    pub literal: bool,
    /// The node holds no shell source text (a bare expansion or substitution).
    pub opaque: bool,
}

pub(super) struct Cmd<'a> {
    /// Command name first, then arguments.
    pub words: &'a [Arg],
    /// The command reads the previous pipeline stage on stdin.
    pub piped: bool,
    /// A heredoc or here-string feeds the command.
    pub here: bool,
}

pub(super) enum Outcome {
    Allow,
    Deny(Rule),
    /// Shell source the command runs; the caller parses and walks it.
    Reparse(String),
}

fn base(name: &str) -> &str {
    let name = name.trim_start_matches('\\');
    name.rsplit('/').next().unwrap_or(name)
}

/// Collapse `//`, `/./`, and `..` in an absolute path without touching the filesystem.
fn normalize_path(text: &str) -> String {
    if !text.starts_with('/') {
        return text.to_string();
    }
    let mut parts: Vec<&str> = Vec::new();
    for seg in text.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            s => parts.push(s),
        }
    }
    format!("/{}", parts.join("/"))
}

pub(super) fn is_device(text: &str) -> bool {
    let is = |s: &str| {
        let s = normalize_path(s);
        matches!(s.as_str(), "/dev/stdout" | "/dev/stderr" | "/dev/tty")
            || s.starts_with("/dev/fd/")
            || s.starts_with("/proc/self/fd/")
    };
    is(text) || text.split_once('=').is_some_and(|(_, v)| is(v))
}

pub(super) fn check(cmd: &Cmd) -> Outcome {
    if cmd.words.iter().any(|a| is_device(&a.text)) {
        return Outcome::Deny(DEVICE_PATH);
    }
    if cmd
        .words
        .first()
        .is_some_and(|a| base(&a.text) == "command")
        && cmd
            .words
            .get(1)
            .is_some_and(|a| matches!(a.text.as_str(), "-v" | "-V"))
    {
        return Outcome::Allow;
    }
    let (words, xargs) = unwrap(cmd.words);
    let Some(first) = words.first() else {
        return Outcome::Allow;
    };
    let name = base(&first.text);
    let args = &words[1..];
    if name == "eval" {
        return reparse(args);
    }
    if let Some(spec) = READERS.iter().find(|r| r.name == name) {
        return read(spec, args, xargs);
    }
    if let Some(spec) = searcher(name) {
        return search(name, spec, args, cmd, xargs);
    }
    match name {
        "tr" => tr(args, xargs),
        "tee" => tee(args),
        "patch" | "dd" | "truncate" => Outcome::Deny(static_name(name)),
        "find" => find(args),
        "git" => git(args),
        "bash" | "sh" | "zsh" | "dash" | "ksh" => shell(args),
        _ => match interpreter_family(name) {
            Some(family) => interpreter(family, args, cmd),
            None => Outcome::Allow,
        },
    }
}

fn static_name(name: &str) -> Rule {
    match name {
        "patch" => "patch",
        "dd" => "dd",
        _ => "truncate",
    }
}

fn reparse(args: &[Arg]) -> Outcome {
    if args.iter().any(|a| a.opaque) {
        return Outcome::Deny(UNRESOLVED);
    }
    let joined = args
        .iter()
        .map(|a| a.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    Outcome::Reparse(joined)
}

/// Strip wrapper commands. Returns the wrapped command and whether `xargs` feeds it operands.
fn unwrap(mut words: &[Arg]) -> (&[Arg], bool) {
    let mut xargs = false;
    while let Some(first) = words.first() {
        let name = base(&first.text);
        let rest = &words[1..];
        let mut i = 0;
        match name {
            "env" | "command" | "builtin" | "exec" | "nohup" | "time" | "setsid" | "stdbuf"
            | "nice" | "ionice" | "timeout" | "sudo" | "doas" => {
                while let Some(arg) = rest.get(i) {
                    let t = arg.text.as_str();
                    if t.len() > 1 && t.starts_with('-') {
                        i += 1 + usize::from(wrapper_takes_value(name, t));
                    } else if name == "env" && t.contains('=') && !t.starts_with('=') {
                        i += 1;
                    } else {
                        break;
                    }
                }
                if name == "timeout" && i < rest.len() {
                    i += 1;
                }
            }
            "xargs" => {
                xargs = true;
                while let Some(arg) = rest.get(i) {
                    let t = arg.text.as_str();
                    if t.len() > 1 && t.starts_with('-') {
                        let separate =
                            matches!(t, "-I" | "-n" | "-P" | "-d" | "-E" | "-L" | "-s" | "-a");
                        i += 1 + usize::from(separate);
                    } else {
                        break;
                    }
                }
            }
            _ => break,
        }
        words = rest.get(i..).unwrap_or(&[]);
    }
    (words, xargs)
}

fn wrapper_takes_value(name: &str, flag: &str) -> bool {
    matches!(
        (name, flag),
        ("nice", "-n")
            | ("ionice", "-c" | "-n")
            | ("env", "-u" | "-C")
            | ("timeout", "-s" | "-k")
            | (
                "sudo" | "doas",
                "-u" | "-g" | "-C" | "-h" | "-p" | "-r" | "-t" | "-U"
            )
    )
}

// ---------------------------------------------------------------------------
// Option splitting

struct Parsed<'a> {
    flags: Vec<String>,
    operands: Vec<&'a Arg>,
}

impl Parsed<'_> {
    fn has(&self, flag: &str) -> bool {
        self.flags.iter().any(|f| f == flag)
    }
    fn has_any(&self, flags: &[&str]) -> bool {
        flags.iter().any(|f| self.has(f))
    }
}

/// Split arguments into flags and operands. `valued_short` lists short flags that take a value.
/// `valued_long` lists long flags (with `--`) and how many values each takes.
fn split<'a>(args: &'a [Arg], valued_short: &str, valued_long: &[(&str, usize)]) -> Parsed<'a> {
    let mut parsed = Parsed {
        flags: Vec::new(),
        operands: Vec::new(),
    };
    let mut i = 0;
    while i < args.len() {
        let text = args[i].text.as_str();
        i += 1;
        if text == "--" {
            parsed.operands.extend(args[i..].iter());
            break;
        }
        if text == "-" || !text.starts_with('-') {
            parsed.operands.push(&args[i - 1]);
        } else if let Some(long) = text.strip_prefix("--") {
            let (key, has_value) = long
                .split_once('=')
                .map_or((long, false), |(k, _)| (k, true));
            let flag = format!("--{key}");
            if !has_value {
                if let Some((_, n)) = valued_long.iter().find(|(k, _)| *k == flag) {
                    i += n;
                }
            }
            parsed.flags.push(flag);
        } else {
            let chars: Vec<char> = text[1..].chars().collect();
            for (k, ch) in chars.iter().enumerate() {
                parsed.flags.push(format!("-{ch}"));
                if valued_short.contains(*ch) {
                    if k + 1 == chars.len() {
                        i += 1;
                    }
                    break;
                }
            }
        }
    }
    parsed
}

// ---------------------------------------------------------------------------
// Readers

struct Reader {
    name: &'static str,
    valued_short: &'static str,
    valued_long: &'static [(&'static str, usize)],
    /// Flags that supply the program; without one, the first operand is the program.
    script_flags: Option<&'static [&'static str]>,
}

const fn reader(name: &'static str, valued_short: &'static str) -> Reader {
    Reader {
        name,
        valued_short,
        valued_long: &[],
        script_flags: None,
    }
}

const SED_FLAGS: &[&str] = &["-e", "-f", "--expression", "--file"];
const AWK_FLAGS: &[&str] = &["-f", "-e", "--file", "--source"];
const AWK_LONG: &[(&str, usize)] = &[
    ("--field-separator", 1),
    ("--assign", 1),
    ("--file", 1),
    ("--source", 1),
];
const SED_LONG: &[(&str, usize)] = &[("--expression", 1), ("--file", 1), ("--line-length", 1)];
const JQ_LONG: &[(&str, usize)] = &[
    ("--arg", 2),
    ("--argjson", 2),
    ("--slurpfile", 2),
    ("--rawfile", 2),
    ("--indent", 1),
    ("--from-file", 1),
];
const YQ_LONG: &[(&str, usize)] = &[
    ("--input-format", 1),
    ("--output-format", 1),
    ("--indent", 1),
    ("--arg", 2),
];

const READERS: &[Reader] = &[
    reader("cat", ""),
    Reader {
        valued_long: &[("--separator", 1)],
        ..reader("tac", "s")
    },
    Reader {
        valued_long: &[("--lines", 1), ("--bytes", 1)],
        ..reader("head", "nc")
    },
    Reader {
        valued_long: &[
            ("--lines", 1),
            ("--bytes", 1),
            ("--sleep-interval", 1),
            ("--pid", 1),
        ],
        ..reader("tail", "ncs")
    },
    reader("less", "bhjkoOpPtTxyz#"),
    reader("more", "n"),
    reader("nl", "bdfhilnsvw"),
    Reader {
        valued_long: &[
            ("--language", 1),
            ("--highlight-line", 1),
            ("--line-range", 1),
            ("--style", 1),
            ("--theme", 1),
            ("--tabs", 1),
            ("--wrap", 1),
        ],
        ..reader("bat", "lHmr")
    },
    Reader {
        valued_long: SED_LONG,
        script_flags: Some(SED_FLAGS),
        ..reader("sed", "efl")
    },
    Reader {
        valued_long: SED_LONG,
        script_flags: Some(SED_FLAGS),
        ..reader("gsed", "efl")
    },
    Reader {
        valued_long: AWK_LONG,
        script_flags: Some(AWK_FLAGS),
        ..reader("awk", "Fvfei")
    },
    Reader {
        valued_long: AWK_LONG,
        script_flags: Some(AWK_FLAGS),
        ..reader("gawk", "Fvfei")
    },
    Reader {
        valued_long: AWK_LONG,
        script_flags: Some(AWK_FLAGS),
        ..reader("mawk", "Fvfei")
    },
    Reader {
        valued_long: &[
            ("--key", 1),
            ("--field-separator", 1),
            ("--output", 1),
            ("--buffer-size", 1),
        ],
        ..reader("sort", "ktTSo")
    },
    reader("uniq", "fsw"),
    reader("cut", "dfcb"),
    reader("wc", ""),
    reader("diff", "UCLFIxX"),
    reader("cmp", "ni"),
    reader("od", "AjNStw"),
    reader("xxd", "lcsgo"),
    reader("hexdump", "nseLf"),
    reader("strings", "netT"),
    reader("base64", "w"),
    Reader {
        valued_long: JQ_LONG,
        script_flags: Some(&["-f", "--from-file"]),
        ..reader("jq", "fL")
    },
    Reader {
        valued_long: YQ_LONG,
        script_flags: Some(&[]),
        ..reader("yq", "poI")
    },
];

fn read(spec: &Reader, args: &[Arg], xargs: bool) -> Outcome {
    let name = spec.name;
    let p = split(args, spec.valued_short, spec.valued_long);
    if matches!(name, "sed" | "gsed") && p.has_any(&["-i", "--in-place"]) {
        return Outcome::Deny(SED_I);
    }
    if name.ends_with("awk") && awk_inplace(args) {
        return Outcome::Deny(AWK_INPLACE);
    }
    let mut operands = p.operands.iter().copied();
    if let Some(script_flags) = spec.script_flags {
        if !p.has_any(script_flags) {
            let first = operands.next();
            let yq_sub = name == "yq"
                && first
                    .is_some_and(|a| matches!(a.text.as_str(), "e" | "ea" | "eval" | "eval-all"));
            if yq_sub {
                operands.next();
            }
        }
    }
    let files = operands.filter(|a| {
        let t = a.text.as_str();
        let skip_plus = matches!(name, "tail" | "less" | "more") && t.starts_with('+');
        let awk_var = name.ends_with("awk") && is_assignment(t);
        t != "-" && !skip_plus && !awk_var
    });
    if files.count() > 0 || xargs {
        Outcome::Deny(spec.name)
    } else {
        Outcome::Allow
    }
}

fn is_assignment(text: &str) -> bool {
    text.split_once('=').is_some_and(|(k, _)| {
        !k.is_empty()
            && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            && !k.starts_with(|c: char| c.is_ascii_digit())
    })
}

fn awk_inplace(args: &[Arg]) -> bool {
    args.iter().enumerate().any(|(i, a)| {
        a.text == "-iinplace"
            || (a.text == "-i" && args.get(i + 1).is_some_and(|n| n.text == "inplace"))
    })
}

/// `tr` reads only stdin; operands past the character sets are files.
fn tr(args: &[Arg], xargs: bool) -> Outcome {
    let p = split(args, "", &[]);
    if p.operands.len() >= 3 || xargs {
        Outcome::Deny("tr")
    } else {
        Outcome::Allow
    }
}

// ---------------------------------------------------------------------------
// Searchers

struct Searcher {
    valued_short: &'static str,
    valued_long: &'static [(&'static str, usize)],
    /// `Some(flags)`: recursive only with one of the flags. `None`: always recursive.
    recursive: Option<&'static [&'static str]>,
    listing: &'static [&'static str],
}

const GREP: Searcher = Searcher {
    valued_short: "efmABCdD",
    valued_long: &[
        ("--regexp", 1),
        ("--file", 1),
        ("--max-count", 1),
        ("--context", 1),
        ("--after-context", 1),
        ("--before-context", 1),
        ("--directories", 1),
        ("--devices", 1),
        ("--label", 1),
        ("--include", 1),
        ("--exclude", 1),
        ("--exclude-dir", 1),
        ("--binary-files", 1),
    ],
    recursive: Some(&["-r", "-R", "--recursive", "--dereference-recursive"]),
    listing: &[],
};

const RG: Searcher = Searcher {
    valued_short: "efgtTmABCjMrdE",
    valued_long: &[
        ("--regexp", 1),
        ("--file", 1),
        ("--glob", 1),
        ("--iglob", 1),
        ("--type", 1),
        ("--type-not", 1),
        ("--type-add", 1),
        ("--type-clear", 1),
        ("--max-count", 1),
        ("--context", 1),
        ("--after-context", 1),
        ("--before-context", 1),
        ("--threads", 1),
        ("--max-columns", 1),
        ("--replace", 1),
        ("--max-depth", 1),
        ("--encoding", 1),
        ("--sort", 1),
        ("--sortr", 1),
        ("--colors", 1),
        ("--color", 1),
        ("--pre", 1),
        ("--pre-glob", 1),
        ("--max-filesize", 1),
        ("--engine", 1),
    ],
    recursive: None,
    listing: &["--files", "--type-list"],
};

const AG: Searcher = Searcher {
    valued_short: "ABCGgm",
    valued_long: &[
        ("--file-search-regex", 1),
        ("--ignore", 1),
        ("--ignore-dir", 1),
        ("--context", 1),
        ("--after", 1),
        ("--before", 1),
        ("--max-count", 1),
        ("--depth", 1),
        ("--pager", 1),
    ],
    recursive: None,
    listing: &["-g", "--list-file-types"],
};

const ACK: Searcher = Searcher {
    valued_short: "ABCm",
    valued_long: &[
        ("--type", 1),
        ("--ignore-dir", 1),
        ("--ignore-file", 1),
        ("--match", 1),
        ("--context", 1),
        ("--after-context", 1),
        ("--before-context", 1),
        ("--max-count", 1),
        ("--output", 1),
    ],
    recursive: None,
    listing: &["-f", "-g", "--help-types"],
};

fn searcher(name: &str) -> Option<&'static Searcher> {
    match name {
        "grep" | "egrep" | "fgrep" => Some(&GREP),
        "rg" => Some(&RG),
        "ag" => Some(&AG),
        "ack" => Some(&ACK),
        _ => None,
    }
}

fn searcher_rule(name: &str) -> Rule {
    match name {
        "egrep" => "egrep",
        "fgrep" => "fgrep",
        "rg" => "rg",
        "ag" => "ag",
        "ack" => "ack",
        _ => "grep",
    }
}

fn is_prefix_of(word: &str, text: &str, min: usize) -> bool {
    text.len() >= min && word.starts_with(text)
}

fn recurse_value(v: &str) -> bool {
    !v.is_empty() && "recurse".starts_with(v)
}

/// Structural grep recursion: abbreviated `--rec*`, `--dir*=rec*`, and `-d rec*` forms.
fn directories_recurse(args: &[Arg]) -> bool {
    args.iter().enumerate().any(|(i, a)| {
        let t = a.text.as_str();
        let next = args.get(i + 1).map_or("", |n| n.text.as_str());
        if let Some(long) = t.strip_prefix("--") {
            let (name, value) = match long.split_once('=') {
                Some((n, v)) => (n, Some(v)),
                None => (long, None),
            };
            return is_prefix_of("recursive", name, 3) && value.is_none()
                || is_prefix_of("dereference-recursive", name, 3) && value.is_none()
                || is_prefix_of("directories", name, 3) && recurse_value(value.unwrap_or(next));
        }
        let Some(cluster) = t.strip_prefix('-') else {
            return false;
        };
        for (idx, c) in cluster.char_indices() {
            if "efmABCD".contains(c) {
                return false;
            }
            if c == 'd' {
                let rest = &cluster[idx + 1..];
                return recurse_value(if rest.is_empty() { next } else { rest });
            }
        }
        false
    })
}

fn search(name: &str, spec: &Searcher, args: &[Arg], cmd: &Cmd, xargs: bool) -> Outcome {
    let rule = searcher_rule(name);
    let p = split(args, spec.valued_short, spec.valued_long);
    if p.has_any(spec.listing) {
        return Outcome::Allow;
    }
    if let Some(only) = args.first() {
        let probe = match only.text.as_str() {
            "--version" | "--help" => true,
            "-V" => matches!(name, "grep" | "egrep" | "fgrep" | "rg"),
            "-h" => matches!(name, "rg" | "ag"),
            _ => false,
        };
        if probe {
            return Outcome::Allow;
        }
    }
    let pattern_given = p.has_any(&["-e", "-f", "--regexp", "--file"]);
    let skip = usize::from(!pattern_given);
    if p.operands.iter().skip(skip).any(|a| a.text != "-") {
        return Outcome::Deny(rule);
    }
    let recursive_flag = spec
        .recursive
        .is_some_and(|flags| p.has_any(flags) || directories_recurse(args));
    let stdin_fed = cmd.piped || cmd.here;
    if recursive_flag || xargs || (spec.recursive.is_none() && !stdin_fed) {
        return Outcome::Deny(rule);
    }
    Outcome::Allow
}

// ---------------------------------------------------------------------------
// Writers and other named commands

fn tee(args: &[Arg]) -> Outcome {
    let p = split(args, "", &[]);
    if p.operands
        .iter()
        .any(|a| a.text != "-" && a.text != "/dev/null")
    {
        Outcome::Deny("tee")
    } else {
        Outcome::Allow
    }
}

fn find(args: &[Arg]) -> Outcome {
    for a in args {
        let rule = match a.text.as_str() {
            "-exec" => "find -exec",
            "-execdir" => "find -execdir",
            "-ok" => "find -ok",
            "-okdir" => "find -okdir",
            _ => continue,
        };
        return Outcome::Deny(rule);
    }
    Outcome::Allow
}

fn git(args: &[Arg]) -> Outcome {
    let mut i = 0;
    while let Some(arg) = args.get(i) {
        let t = arg.text.as_str();
        if !t.starts_with('-') {
            break;
        }
        let separate = matches!(t, "-C" | "-c" | "--git-dir" | "--work-tree" | "--namespace");
        i += 1 + usize::from(separate);
    }
    let Some(sub) = args.get(i) else {
        return Outcome::Allow;
    };
    let rest = &args[i + 1..];
    match sub.text.as_str() {
        "grep" => Outcome::Deny(GIT_GREP),
        "apply" => Outcome::Deny(GIT_APPLY),
        "cat-file" => Outcome::Deny(GIT_CAT_FILE),
        "show"
            if rest
                .iter()
                .any(|a| !a.text.starts_with('-') && a.text.contains(':')) =>
        {
            Outcome::Deny(GIT_SHOW)
        }
        _ => Outcome::Allow,
    }
}

// ---------------------------------------------------------------------------
// Shells

fn shell(args: &[Arg]) -> Outcome {
    let mut c_mode = false;
    let mut informational = false;
    let mut operand = None;
    let mut i = 0;
    while let Some(arg) = args.get(i) {
        let t = arg.text.as_str();
        i += 1;
        if t == "--" {
            operand = (i < args.len()).then_some(i);
            break;
        }
        if matches!(t, "--version" | "--help") {
            informational = true;
        } else if matches!(t, "--rcfile" | "--init-file" | "-o" | "+o" | "-O" | "+O") {
            i += 1;
        } else if t.len() > 1 && (t.starts_with('-') || t.starts_with('+')) {
            c_mode |= t.starts_with('-') && !t.starts_with("--") && t.contains('c');
        } else {
            operand = Some(i - 1);
            break;
        }
    }
    match operand {
        Some(k) if c_mode => reparse(&args[k..=k]),
        Some(_) => Outcome::Allow,
        None if c_mode || informational => Outcome::Allow,
        None => Outcome::Deny(SHELL_STDIN),
    }
}

// ---------------------------------------------------------------------------
// Inline interpreters

struct Interp {
    inline_short: &'static str,
    /// Short flags that take the rest of the cluster, or the next argument.
    valued_short: &'static str,
    /// Short flags that take only the rest of the cluster.
    rest_short: &'static str,
    inline_long: &'static [&'static str],
    valued_long: &'static [&'static str],
}

fn interpreter_family(name: &str) -> Option<Rule> {
    let stem = name.trim_end_matches(|c: char| c.is_ascii_digit() || c == '.');
    match stem {
        "python" => Some("python"),
        "perl" => Some("perl"),
        "ruby" => Some("ruby"),
        "node" | "nodejs" => Some("node"),
        "deno" => Some("deno"),
        "bun" => Some("bun"),
        "php" => Some("php"),
        _ => None,
    }
}

fn interp_spec(family: &str) -> Interp {
    let none = Interp {
        inline_short: "",
        valued_short: "",
        rest_short: "",
        inline_long: &[],
        valued_long: &[],
    };
    match family {
        "python" => Interp {
            inline_short: "c",
            valued_short: "WX",
            ..none
        },
        "perl" => Interp {
            inline_short: "eE",
            valued_short: "I",
            rest_short: "MmCFxdDl0V",
            ..none
        },
        "ruby" => Interp {
            inline_short: "e",
            valued_short: "IrCEFx",
            rest_short: "0",
            ..none
        },
        "node" => Interp {
            inline_short: "ep",
            valued_short: "rC",
            inline_long: &["--eval", "--print"],
            valued_long: &[
                "--require",
                "--import",
                "--loader",
                "--experimental-loader",
                "--conditions",
            ],
            ..none
        },
        "bun" => Interp {
            inline_short: "ep",
            valued_short: "r",
            inline_long: &["--eval", "--print"],
            valued_long: &["--preload"],
            ..none
        },
        "php" => Interp {
            inline_short: "rRBE",
            valued_short: "dcz",
            ..none
        },
        _ => none,
    }
}

fn interpreter(family: Rule, args: &[Arg], cmd: &Cmd) -> Outcome {
    if cmd.here {
        return Outcome::Deny(family);
    }
    let spec = interp_spec(family);
    let mut module: Option<String> = None;
    let mut module_end = args.len();
    let mut script: Option<&str> = None;
    let mut informational = false;
    let mut i = 0;
    'args: while i < args.len() {
        let t = args[i].text.as_str();
        i += 1;
        if t == "-" {
            return Outcome::Deny(family);
        }
        if t == "--" {
            script = args.get(i).map(|a| a.text.as_str());
            break;
        }
        if let Some(long) = t.strip_prefix("--") {
            let (key, has_value) = long
                .split_once('=')
                .map_or((long, false), |(k, _)| (k, true));
            let flag = format!("--{key}");
            if spec.inline_long.contains(&flag.as_str()) {
                return Outcome::Deny(family);
            }
            informational |= matches!(key, "version" | "help");
            if family == "node" && key.starts_with("test") {
                script = Some("--test");
            }
            if !has_value && spec.valued_long.contains(&flag.as_str()) {
                i += 1;
            }
            continue;
        }
        if let Some(cluster) = t.strip_prefix('-') {
            informational |= matches!(t, "-V" | "-v" | "-h" | "-?");
            let chars: Vec<char> = cluster.chars().collect();
            for (k, ch) in chars.iter().enumerate() {
                if family == "perl" && *ch == 'i' {
                    return Outcome::Deny(PERL_I);
                }
                if spec.inline_short.contains(*ch) {
                    return Outcome::Deny(family);
                }
                if family == "python" && *ch == 'm' {
                    let attached: String = chars[k + 1..].iter().collect();
                    if attached.is_empty() {
                        module = args.get(i).map(|a| a.text.clone());
                        module_end = i + 1;
                    } else {
                        module = Some(attached);
                        module_end = i;
                    }
                    break 'args;
                }
                if spec.rest_short.contains(*ch) {
                    break;
                }
                if spec.valued_short.contains(*ch) {
                    if k + 1 == chars.len() {
                        i += 1;
                    }
                    break;
                }
            }
            continue;
        }
        script = Some(t);
        break;
    }
    if let Some(module) = module {
        let leftover = args.get(module_end..).unwrap_or(&[]);
        let reads_file = leftover.iter().any(|a| !a.text.starts_with('-'));
        return if module == "json.tool" && reads_file {
            Outcome::Deny(JSON_TOOL)
        } else {
            Outcome::Allow
        };
    }
    match (family, script) {
        ("deno", Some("eval")) => Outcome::Deny(family),
        ("deno" | "bun", _) | (_, Some(_)) => Outcome::Allow,
        _ if informational => Outcome::Allow,
        _ => Outcome::Deny(family),
    }
}
