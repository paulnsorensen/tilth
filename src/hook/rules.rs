//! The deny-list: one simple command in, an outcome out.

use super::hints::{
    Rule, AWK_INPLACE, DEBUGGER_SCRIPT, DEVICE_PATH, GIT_APPLY, GIT_CAT_FILE, GIT_CONFIG, GIT_GREP,
    GIT_SHOW, INPUT_REDIRECT, JSON_TOOL, MAKE_EVAL, OUTPUT_REDIRECT, PERL_I, PYTHON_M, SED_I,
    SHELL_STDIN, SQLITE_IO, STDIN_REPL, UNRESOLVED,
};

/// One word of a simple command, with quotes removed.
#[derive(Clone)]
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
    /// Leading `NAME=value` assignments before the command name.
    pub assigns: &'a [Arg],
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

/// Returns the final path component after stripping leading shell-escape backslashes.
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

/// Detects blocked device paths, including normalized paths and assignment values.
pub(super) fn is_device(text: &str) -> bool {
    let is = |s: &str| {
        let s = normalize_path(s);
        matches!(
            s.as_str(),
            "/dev/stdin" | "/dev/stdout" | "/dev/stderr" | "/dev/tty"
        ) || s.starts_with("/dev/fd/")
            || s.starts_with("/proc/self/fd/")
    };
    is(text) || text.split_once('=').is_some_and(|(_, v)| is(v))
}

/// Applies device, wrapper, and command rules to one simple command.
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
    check_from(cmd, cmd.words, false)
}

/// Checks the command in `words`, a tail of `cmd.words`, after its wrappers and tool runners.
fn check_from(cmd: &Cmd, words: &[Arg], xargs: bool) -> Outcome {
    let (words, xargs) = match unwrap(cmd, words, xargs) {
        Ok(unwrapped) => unwrapped,
        Err(outcome) => return outcome,
    };
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
        "tee" => writer(args, xargs, "tee"),
        "sponge" => writer(args, xargs, "sponge"),
        "patch" | "dd" | "truncate" | "ed" | "ex" => Outcome::Deny(static_name(name)),
        "vi" | "vim" | "nvim" | "view" | "vimdiff" => vim(args),
        "find" => find(args),
        "fd" | "fdfind" => fd(args),
        "git" => {
            if env_assigns(cmd, words)
                .iter()
                .any(|a| git_env_runs_command(&a.text))
            {
                return Outcome::Deny(GIT_CONFIG);
            }
            git(args)
        }
        "ast-grep" | "sg" => ast_grep(args, cmd, xargs),
        "bash" | "sh" | "zsh" | "dash" | "ksh" | "fish" | "tcsh" | "csh" => {
            shell(args, name == "fish")
        }
        "trap" => trap(args),
        "script" => script(args),
        "curl" => curl(args),
        "tar" => tar(args),
        "source" | "." => source(args),
        _ => match interpreter_family(name) {
            Some(family) => interpreter(family, args, cmd),
            None => stdin_reader(name, args, &env_assigns(cmd, words)),
        },
    }
}

/// Returns the static denial rule for `patch`, `dd`, or `truncate`.
fn static_name(name: &str) -> Rule {
    match name {
        "patch" => "patch",
        "dd" => "dd",
        "ed" => "ed",
        "ex" => "ex",
        _ => "truncate",
    }
}

/// Joins visible arguments as shell source, denying opaque expansions or substitutions.
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

/// The options a wrapper command accepts. An option outside these sets fails closed.
struct WrapOpts {
    short_val: &'static str,
    short_flag: &'static str,
    long_val: &'static [&'static str],
    long_flag: &'static [&'static str],
}

/// Returns the supported flag and value-option sets for a wrapper command.
fn wrapper_opts(name: &str) -> WrapOpts {
    let (short_val, short_flag, long_val, long_flag): (_, _, &[&str], &[&str]) = match name {
        "env" => (
            "uCaP",
            "i0v",
            &["--unset", "--chdir", "--argv0", "--default-signal"],
            &[
                "--ignore-environment",
                "--null",
                "--debug",
                "--block-signal",
                "--ignore-signal",
                "--list-signal-handling",
            ],
        ),
        "nice" => ("n", "0123456789", &["--adjustment"], &[]),
        "ionice" => (
            "cnpPu",
            "t",
            &["--class", "--classdata", "--pid", "--pgid", "--uid"],
            &["--ignore"],
        ),
        "timeout" => (
            "sk",
            "vf",
            &["--signal", "--kill-after"],
            &["--foreground", "--preserve-status", "--verbose"],
        ),
        "sudo" => (
            "ugChprtUDRT",
            "EHisbkKnPSAlVv",
            &[
                "--user",
                "--group",
                "--close-from",
                "--host",
                "--prompt",
                "--role",
                "--type",
                "--other-user",
                "--chdir",
                "--chroot",
                "--command-timeout",
            ],
            &[
                "--preserve-env",
                "--set-home",
                "--login",
                "--shell",
                "--background",
                "--reset-timestamp",
                "--remove-timestamp",
                "--non-interactive",
                "--preserve-groups",
                "--stdin",
                "--askpass",
                "--version",
            ],
        ),
        "doas" => ("uC", "nsL", &[], &[]),
        "stdbuf" => ("ioe", "", &["--input", "--output", "--error"], &[]),
        "setsid" => ("", "cfw", &[], &["--ctty", "--fork", "--wait"]),
        "time" => (
            "fo",
            "pavlV",
            &["--format", "--output"],
            &["--portability", "--append", "--verbose"],
        ),
        "exec" => ("a", "cl", &[], &[]),
        "flock" => (
            "wE",
            "sxunoF",
            &["--timeout", "--conflict-exit-code"],
            &[
                "--shared",
                "--exclusive",
                "--unlock",
                "--nonblock",
                "--nb",
                "--no-fork",
                "--close",
                "--verbose",
            ],
        ),
        "taskset" => ("", "capr", &[], &["--cpu-list", "--all-tasks", "--pid"]),
        "strace" => (
            "eopsuEPSaIOXb",
            "cCdDfFhiknqrtTvVwxyzZ",
            &[
                "--output",
                "--trace",
                "--signal",
                "--status",
                "--inject",
                "--fault",
                "--env",
                "--string-limit",
                "--attach",
                "--user",
                "--columns",
            ],
            &[
                "--follow-forks",
                "--summary",
                "--summary-only",
                "--debug",
                "--no-abbrev",
            ],
        ),
        "command" => ("", "pvV", &[], &[]),
        "xargs" => (
            "INPnLdEsaRS",
            "0tprxoeilhV",
            &[
                "--max-args",
                "--max-procs",
                "--max-chars",
                "--max-lines",
                "--delimiter",
                "--eof",
                "--arg-file",
                "--process-slot-var",
            ],
            &[
                "--null",
                "--verbose",
                "--interactive",
                "--no-run-if-empty",
                "--exit",
                "--open-tty",
                "--replace",
                "--version",
                "--help",
            ],
        ),
        _ => ("", "", &[], &[]),
    };
    WrapOpts {
        short_val,
        short_flag,
        long_val,
        long_flag,
    }
}

/// A `VAR=value` word in a wrapper's argument list.
fn is_assign_word(t: &str) -> bool {
    t.contains('=') && !t.starts_with(['=', '-'])
}

/// The `NAME=value` words that set the environment of the command in `words`, a tail of
/// `cmd.words`: the leading assignments, and those that wrappers such as `env` take.
fn env_assigns<'a>(cmd: &Cmd<'a>, words: &[Arg]) -> Vec<&'a Arg> {
    let wrapped = &cmd.words[..cmd.words.len() - words.len()];
    cmd.assigns
        .iter()
        .chain(wrapped.iter().filter(|a| is_assign_word(&a.text)))
        .collect()
}

/// Required positional words a wrapper takes before the wrapped command.
fn wrapper_positionals(name: &str) -> usize {
    usize::from(matches!(name, "timeout" | "flock" | "taskset"))
}

/// Index of the first wrapped-command word, or `None` for an unknown option. One loop
/// consumes, in any order: options (with values), a `--` terminator, `VAR=value` words,
/// and `positionals` required operands. Options stop at `--`.
fn skip_options(args: &[Arg], opts: &WrapOpts, mut positionals: usize) -> Option<usize> {
    let mut i = 0;
    let mut terminated = false;
    while let Some(arg) = args.get(i) {
        let t = arg.text.as_str();
        if t == "--" && !terminated {
            terminated = true;
            i += 1;
        } else if is_assign_word(t) {
            i += 1;
        } else if terminated || t.len() < 2 || !t.starts_with('-') {
            if positionals == 0 {
                break;
            }
            positionals -= 1;
            i += 1;
        } else if let Some(long) = t.strip_prefix("--") {
            let (key, has_value) = long
                .split_once('=')
                .map_or((long, false), |(k, _)| (k, true));
            let flag = format!("--{key}");
            if opts.long_val.contains(&flag.as_str()) {
                i += 1 + usize::from(!has_value);
            } else if opts.long_flag.contains(&flag.as_str()) {
                i += 1;
            } else {
                return None;
            }
        } else {
            let chars: Vec<char> = t[1..].chars().collect();
            i += 1;
            for (k, ch) in chars.iter().enumerate() {
                if opts.short_val.contains(*ch) {
                    i += usize::from(k + 1 == chars.len());
                    break;
                }
                if !opts.short_flag.contains(*ch) {
                    return None;
                }
            }
        }
    }
    Some(i.min(args.len()))
}

/// A wrapper option that reads or writes a file: `xargs -a FILE`, `time -o FILE`.
fn option_file_rule(name: &str, given: &[Arg], opts: &WrapOpts) -> Option<Rule> {
    let (short, long, rule) = match name {
        "xargs" => ('a', "--arg-file", INPUT_REDIRECT),
        "time" | "strace" => ('o', "--output", OUTPUT_REDIRECT),
        _ => return None,
    };
    let next = |i: usize| given.get(i).map(|a| a.text.as_str());
    let mut i = 0;
    while i < given.len() {
        let t = given[i].text.as_str();
        i += 1;
        let mut value = None;
        if t == "--" {
            break;
        } else if let Some(l) = t.strip_prefix("--") {
            let (key, inline) = l.split_once('=').map_or((l, None), |(k, v)| (k, Some(v)));
            let flag = format!("--{key}");
            if flag == long {
                value = inline.or_else(|| next(i));
            } else if inline.is_none() && opts.long_val.contains(&flag.as_str()) {
                i += 1;
            }
        } else if t.len() > 1 && t.starts_with('-') {
            for (k, ch) in t[1..].char_indices() {
                let after = &t[1 + k + ch.len_utf8()..];
                if ch == short {
                    value = if after.is_empty() {
                        next(i)
                    } else {
                        Some(after)
                    };
                    break;
                }
                if opts.short_val.contains(ch) {
                    i += usize::from(after.is_empty());
                    break;
                }
            }
        }
        if let Some(v) = value {
            let harmless = v == "/dev/null" || (name == "xargs" && v == "-");
            return (!harmless).then_some(rule);
        }
    }
    None
}

/// Strip wrapper commands and tool runners. Returns the wrapped command and whether `xargs`
/// feeds it operands. `Err` holds the final outcome: a runner runs shell source or Python
/// itself, or a runner has two readings and the rules checked both.
fn unwrap<'w>(
    cmd: &Cmd,
    mut words: &'w [Arg],
    mut xargs: bool,
) -> Result<(&'w [Arg], bool), Outcome> {
    while let Some(first) = words.first() {
        let name = base(&first.text);
        let rest = &words[1..];
        let i = match name {
            "env" | "command" | "builtin" | "exec" | "nohup" | "time" | "setsid" | "stdbuf"
            | "nice" | "ionice" | "timeout" | "sudo" | "doas" | "xargs" | "flock" | "taskset"
            | "strace" => {
                xargs |= name == "xargs";
                let opts = wrapper_opts(name);
                let i = skip_options(rest, &opts, wrapper_positionals(name))
                    .ok_or(Outcome::Deny(UNRESOLVED))?;
                if let Some(rule) = option_file_rule(name, &rest[..i], &opts) {
                    return Err(Outcome::Deny(rule));
                }
                i
            }
            _ => match RUNNERS
                .iter()
                .find(|r| r.name == name)
                .map(|r| run_target(r, rest))
            {
                Some(Run::At(i)) => i,
                Some(Run::Done(outcome)) => return Err(outcome),
                Some(run @ Run::Either(..)) => return Err(resolve(cmd, rest, run, xargs)),
                Some(Run::No) | None => break,
            },
        };
        words = rest.get(i..).unwrap_or(&[]);
    }
    Ok((words, xargs))
}

/// The outcome of one reading of a runner's arguments `rest`.
fn resolve(cmd: &Cmd, rest: &[Arg], run: Run, xargs: bool) -> Outcome {
    match run {
        Run::At(i) => check_from(cmd, rest.get(i..).unwrap_or(&[]), xargs),
        Run::Done(outcome) => outcome,
        Run::Either(first, second) => either(
            resolve(cmd, rest, *first, xargs),
            resolve(cmd, rest, *second, xargs),
        ),
        // `scan` never returns `No`; only the subcommand search does.
        Run::No => Outcome::Allow,
    }
}

/// Joins two readings of one command. A deny in either reading denies, and the shell source of
/// both readings reparses.
fn either(first: Outcome, second: Outcome) -> Outcome {
    match (first, second) {
        (Outcome::Deny(rule), _) | (_, Outcome::Deny(rule)) => Outcome::Deny(rule),
        (Outcome::Reparse(a), Outcome::Reparse(b)) => Outcome::Reparse(format!("{a}\n{b}")),
        (Outcome::Reparse(a), Outcome::Allow) | (Outcome::Allow, Outcome::Reparse(a)) => {
            Outcome::Reparse(a)
        }
        (Outcome::Allow, Outcome::Allow) => Outcome::Allow,
    }
}

/// How an option word gives its value.
#[derive(Clone, Copy, PartialEq, Eq)]
enum OptValue<'t> {
    /// The value is the next word.
    Next,
    /// The word holds the value (`-cVALUE`, `-c=VALUE`, `--name=VALUE`).
    Inline(&'t str),
}

/// Matches an option word against option names; `None` when no name matches. A short name
/// (`-c`) takes an attached value (`-cVALUE`), and one leading `=` is not part of the value:
/// clap, nopt, and gdb read `-c=VALUE` as `VALUE`. A long name takes `--name=VALUE`. A
/// single-dash long name (`-ex`, in gdb and sqlite3 style) also matches its double-dash spelling
/// (`--ex`).
fn match_opt<'t>(t: &'t str, names: &[&str]) -> Option<OptValue<'t>> {
    names.iter().filter(|n| n.starts_with('-')).find_map(|&n| {
        if n.len() == 2 {
            let after = t.strip_prefix(n)?;
            return Some(match after {
                "" => OptValue::Next,
                _ => OptValue::Inline(after.strip_prefix('=').unwrap_or(after)),
            });
        }
        let t = if n.starts_with("--") {
            t
        } else {
            t.strip_prefix('-')
                .filter(|s| s.starts_with('-'))
                .unwrap_or(t)
        };
        let after = t.strip_prefix(n)?;
        match after {
            "" => Some(OptValue::Next),
            _ => after.strip_prefix('=').map(OptValue::Inline),
        }
    })
}

// ---------------------------------------------------------------------------
// Tool runners

/// Where a tool runner finds the command it runs.
#[derive(Clone, Copy, PartialEq, Eq)]
enum CommandAt {
    /// The first operand, or the words after `--`.
    Operand,
    /// The words after `--`. Operands before it name tools (`mise exec node@20 -- cmd`).
    DashDash,
    /// Only a command option. Without one, the runner starts an interactive shell.
    Option,
}

/// A tool runner (`uv run`, `npx`, `mise exec`) runs another command, and the rules check that
/// command.
///
/// Runners have many options and add more in each release. An option that `valued` does not
/// list counts as a flag, so an unknown option never denies an ordinary command by itself. An
/// unlisted `--long` option can still take the next word as its value, so that word has two
/// readings: the command, or the value. The rules check both readings, and a deny in either one
/// denies.
struct Runner {
    name: &'static str,
    /// Subcommands that run a command. Empty when the runner itself runs it.
    subs: &'static [&'static str],
    /// Words before the subcommand that name a group of subcommands (`uv tool run`).
    groups: &'static [&'static str],
    /// Subcommands that start an interactive shell, which reads stdin (`mise en`).
    shells: &'static [&'static str],
    /// The runner's own commands. When this list is not empty, a first operand outside it and
    /// outside `subs` is a command too: `pnpm cat f` runs `cat`.
    builtins: &'static [&'static str],
    at: CommandAt,
    /// Operands before the command (`direnv exec DIR`).
    positionals: usize,
    /// Options that take a value, before or after the subcommand.
    valued: &'static [&'static str],
    /// Options whose value is shell source (`mise exec -c`, `nix-shell --run`).
    shell: &'static [&'static str],
    /// Options after which the remaining words are the command (`nix develop -c`).
    rest: &'static [&'static str],
    /// Flags that run the command words as shell source (`pnpm exec -c`).
    shell_mode: &'static [&'static str],
    /// The runner always runs the command words as shell source (`devbox run`, `pixi run`).
    shell_words: bool,
    /// hatch: `+NAME=VALUES` words select matrix environments, and the command can start with
    /// `ENV:` (`hatch run test:cov`).
    env_select: bool,
    /// The runner runs Python: `-m MODULE` runs a module, and a `-` command reads stdin.
    python: bool,
    /// With no command, the runner starts a shell that reads stdin (`npx`, `npm exec`).
    stdin_shell: bool,
}

/// Creates a runner that takes its command as the first operand and lists no options.
const fn runner(name: &'static str, subs: &'static [&'static str]) -> Runner {
    Runner {
        name,
        subs,
        groups: &[],
        shells: &[],
        builtins: &[],
        at: CommandAt::Operand,
        positionals: 0,
        valued: &[],
        shell: &[],
        rest: &[],
        shell_mode: &[],
        shell_words: false,
        env_select: false,
        python: false,
        stdin_shell: false,
    }
}

const UV_VALUED: &[&str] = &[
    "-p",
    "--python",
    "--with",
    "--with-editable",
    "--with-requirements",
    "--from",
    "--package",
    "--extra",
    "--no-extra",
    "--group",
    "--no-group",
    "--only-group",
    "--env-file",
    "--directory",
    "--project",
    "--config-file",
    "--cache-dir",
    "--color",
    "--index",
    "--default-index",
    "-i",
    "--index-url",
    "--extra-index-url",
    "-f",
    "--find-links",
    "--index-strategy",
    "--keyring-provider",
    "--resolution",
    "--prerelease",
    "--exclude-newer",
    "--link-mode",
    "-P",
    "--upgrade-package",
    "--reinstall-package",
    "--refresh-package",
    "-C",
    "--config-setting",
    "--python-platform",
    "--python-preference",
    "--allow-insecure-host",
    "-w",
    "--no-editable-package",
    "--upgrade-group",
    "--prerelease-package",
    "--fork-strategy",
    "--exclude-newer-package",
    "--no-sources-package",
    "--config-settings-package",
    "--no-build-isolation-package",
    "--no-build-package",
    "--no-binary-package",
    // uvx only.
    "-c",
    "--constraints",
    "-b",
    "--build-constraints",
    "--overrides",
    "--torch-backend",
];
/// npm reads any config key as an option (`--registry URL`); these are the common valued ones.
const NPM_VALUED: &[&str] = &[
    "-p",
    "--package",
    "-w",
    "--workspace",
    "-C",
    "--prefix",
    "--registry",
    "--cache",
    "--userconfig",
    "--globalconfig",
    "--loglevel",
    "--logs-dir",
    "--script-shell",
    "--shell",
    "--node-options",
    "--before",
    "--tag",
    "--scope",
    "--include",
    "--omit",
    "--location",
    "--install-strategy",
    "--proxy",
    "--https-proxy",
    "--noproxy",
    "--cafile",
    "--otp",
    "--fetch-retries",
    "--fetch-timeout",
];
const NPM_SHELL: &[&str] = &["-c", "--call"];
const PNPM_VALUED: &[&str] = &[
    "-C",
    "--dir",
    "-F",
    "--filter",
    "--package",
    "--reporter",
    "--workspace-concurrency",
];
const PNPM_SHELL_MODE: &[&str] = &["-c", "--shell-mode"];
/// pnpm commands. pnpm runs any other first operand as a script, or else as a command.
const PNPM_BUILTINS: &[&str] = &[
    "add",
    "approve-builds",
    "audit",
    "bin",
    "c",
    "cache",
    "cat-file",
    "cat-index",
    "completion",
    "config",
    "create",
    "dedupe",
    "deploy",
    "dist-tag",
    "doctor",
    "env",
    "fetch",
    "find-hash",
    "get",
    "help",
    "i",
    "ignored-builds",
    "import",
    "info",
    "init",
    "install",
    "install-test",
    "it",
    "la",
    "licenses",
    "link",
    "list",
    "ll",
    "ln",
    "login",
    "logout",
    "ls",
    "outdated",
    "pack",
    "patch",
    "patch-commit",
    "patch-remove",
    "prune",
    "publish",
    "rb",
    "rebuild",
    "remove",
    "restart",
    "rm",
    "root",
    "run",
    "run-script",
    "sbom",
    "search",
    "self-update",
    "server",
    "set",
    "setup",
    "show",
    "start",
    "stop",
    "store",
    "t",
    "test",
    "tst",
    "un",
    "uninstall",
    "unlink",
    "up",
    "update",
    "upgrade",
    "v",
    "version",
    "view",
    "whoami",
    "why",
];
const BUN_VALUED: &[&str] = &[
    "-p",
    "--package",
    "--cwd",
    "-r",
    "--preload",
    "-c",
    "--config",
    "--env-file",
    "-F",
    "--filter",
    "--elide-lines",
    "--shell",
];
const CONDA_VALUED: &[&str] = &[
    "-n",
    "--name",
    "-p",
    "--prefix",
    "--cwd",
    "-a",
    "--attach",
    "-e",
    "--env",
    "-r",
    "--root-prefix",
    "--label",
];
const NIX_VALUED: &[&str] = &[
    "--extra-experimental-features",
    "--experimental-features",
    "--log-format",
    "--store",
    "--eval-store",
    "-I",
    "--include",
    "-f",
    "--file",
    "--expr",
    "--profile",
    "--phase",
];

const RUNNERS: &[Runner] = &[
    Runner {
        at: CommandAt::DashDash,
        shells: &["en"],
        valued: &[
            "-C",
            "--cd",
            "-E",
            "--env",
            "-j",
            "--jobs",
            "-P",
            "--profile",
        ],
        shell: &["-c", "--command"],
        ..runner("mise", &["exec", "x"])
    },
    Runner {
        groups: &["tool"],
        valued: UV_VALUED,
        python: true,
        ..runner("uv", &["run"])
    },
    Runner {
        valued: UV_VALUED,
        ..runner("uvx", &[])
    },
    Runner {
        valued: NPM_VALUED,
        shell: NPM_SHELL,
        stdin_shell: true,
        ..runner("npx", &[])
    },
    Runner {
        valued: NPM_VALUED,
        shell: NPM_SHELL,
        stdin_shell: true,
        ..runner("npm", &["exec", "x"])
    },
    Runner {
        shells: &["shell"],
        valued: &["-C", "--directory", "-P", "--project"],
        ..runner("poetry", &["run"])
    },
    Runner {
        valued: &["--gemfile"],
        ..runner("bundle", &["exec"])
    },
    Runner {
        groups: &["recursive", "m", "multi"],
        builtins: PNPM_BUILTINS,
        valued: PNPM_VALUED,
        shell_mode: PNPM_SHELL_MODE,
        ..runner("pnpm", &["exec", "dlx"])
    },
    Runner {
        valued: PNPM_VALUED,
        shell_mode: PNPM_SHELL_MODE,
        ..runner("pnpx", &[])
    },
    Runner {
        valued: &["-p", "--package", "--cwd"],
        ..runner("yarn", &["exec", "dlx"])
    },
    Runner {
        valued: BUN_VALUED,
        ..runner("bunx", &[])
    },
    Runner {
        valued: BUN_VALUED,
        ..runner("bun", &["x", "run"])
    },
    Runner {
        positionals: 1,
        ..runner("direnv", &["exec"])
    },
    Runner {
        at: CommandAt::Option,
        valued: NIX_VALUED,
        rest: &["-c", "--command"],
        ..runner("nix", &["develop", "shell"])
    },
    Runner {
        at: CommandAt::Option,
        shell: &["--run", "--command"],
        ..runner("nix-shell", &[])
    },
    runner("asdf", &["exec"]),
    Runner {
        shells: &["shell"],
        valued: &[
            "-e",
            "--environment",
            "--manifest-path",
            "--color",
            "-s",
            "--spec",
            "-w",
            "--with",
            "-c",
            "--channel",
            "-p",
            "--platform",
        ],
        shell_words: true,
        ..runner("pixi", &["run", "exec"])
    },
    Runner {
        valued: CONDA_VALUED,
        ..runner("conda", &["run"])
    },
    Runner {
        valued: CONDA_VALUED,
        ..runner("mamba", &["run"])
    },
    Runner {
        valued: CONDA_VALUED,
        ..runner("micromamba", &["run"])
    },
    Runner {
        shells: &["shell"],
        valued: &["--python", "--pypi-mirror"],
        ..runner("pipenv", &["run"])
    },
    Runner {
        valued: &["-p", "--project", "-c", "--config", "--venv", "--skip"],
        ..runner("pdm", &["run"])
    },
    Runner {
        shells: &["shell"],
        valued: &[
            "-e",
            "--env",
            "-p",
            "--project",
            "--data-dir",
            "--cache-dir",
            "--config",
        ],
        shell_words: true,
        env_select: true,
        ..runner("hatch", &["run"])
    },
    Runner {
        shells: &["shell"],
        valued: &["--pyproject"],
        ..runner("rye", &["run"])
    },
    Runner {
        shells: &["shell"],
        valued: &[
            "-c",
            "--config",
            "-e",
            "--env",
            "--env-file",
            "--environment",
        ],
        shell_words: true,
        ..runner("devbox", &["run"])
    },
];

enum Run {
    /// Not a runner invocation (`mise install`, `bun test`).
    No,
    /// The command starts at this index of the runner's arguments.
    At(usize),
    /// The runner runs shell source or Python itself, with this outcome.
    Done(Outcome),
    /// Two readings of a word after an unlisted option: the command, or the option's value.
    Either(Box<Run>, Box<Run>),
}

/// A runner option word. `--` ends the options instead.
fn is_runner_opt(t: &str) -> bool {
    t.len() > 1 && t.starts_with('-') && t != "--"
}

/// The word turns on shell mode. pnpm reads options with nopt: a long name can be a prefix of
/// the name (`--shell-m`), `--name=VALUE` sets it unless VALUE is `false`, and one-letter flags
/// combine (`-rc`).
fn turns_on_shell_mode(spec: &Runner, t: &str) -> bool {
    spec.shell_mode
        .iter()
        .any(|&flag| match flag.strip_prefix("--") {
            Some(full) => t.strip_prefix("--").is_some_and(|long| {
                let (name, value) = long.split_once('=').unwrap_or((long, "true"));
                !name.is_empty() && full.starts_with(name) && value != "false"
            }),
            None => {
                t == flag
                    || t.strip_prefix('-').is_some_and(|cluster| {
                        cluster.len() > 1
                            && cluster.chars().all(|c| c.is_ascii_alphabetic())
                            && cluster.contains(&flag[1..])
                    })
            }
        })
}

/// The number of words an option uses: two when `valued` lists it and its value is the next
/// word, or when a `true` or `false` follows a shell-mode flag (nopt reads `--shell-mode true`).
fn opt_words(spec: &Runner, args: &[Arg], i: usize) -> usize {
    let t = args[i].text.as_str();
    let valued = match_opt(t, spec.valued) == Some(OptValue::Next);
    let boolean = !t.contains('=')
        && turns_on_shell_mode(spec, t)
        && args
            .get(i + 1)
            .is_some_and(|n| matches!(n.text.as_str(), "true" | "false"));
    1 + usize::from(valued || boolean)
}

/// A `--name` option that the runner table does not list. It can take the next word as its value.
fn unlisted_long(spec: &Runner, t: &str) -> bool {
    t.starts_with("--")
        && !t.contains('=')
        && match_opt(t, spec.valued).is_none()
        && !turns_on_shell_mode(spec, t)
}

/// The outcome of a shell-string option at `args[i]` (`npx -c 'cmd'`), or `None`.
fn shell_option(spec: &Runner, args: &[Arg], i: usize) -> Option<Outcome> {
    let a = &args[i];
    Some(match match_opt(&a.text, spec.shell)? {
        OptValue::Inline(v) => reparse(&[Arg {
            text: v.to_string(),
            literal: a.literal,
            opaque: a.opaque,
        }]),
        OptValue::Next => args
            .get(i + 1)
            .map_or(Outcome::Allow, |n| reparse(std::slice::from_ref(n))),
    })
}

/// Finds the command that a runner invocation runs. Options before the subcommand count too:
/// npm reads `npm -c CMD exec`, and pnpm reads `pnpm -c exec CMD`.
fn run_target(spec: &Runner, args: &[Arg]) -> Run {
    if spec.subs.is_empty() {
        return scan(spec, args, 0, spec.positionals, spec.shell_words, true);
    }
    find_sub(spec, args, 0, spec.shell_words)
}

/// Finds the subcommand from `args[i]`, past options and group words. A runner with `builtins`
/// also runs a first operand that is not one of its commands, so the word after an unlisted
/// option has two readings: the command, or the option's value.
fn find_sub(spec: &Runner, args: &[Arg], mut i: usize, mut shell_mode: bool) -> Run {
    let mut maybe_value = false;
    while let Some(t) = args.get(i).map(|a| a.text.as_str()) {
        if let Some(outcome) = shell_option(spec, args, i) {
            return Run::Done(outcome);
        }
        if is_runner_opt(t) {
            shell_mode |= turns_on_shell_mode(spec, t);
            maybe_value = unlisted_long(spec, t);
            i += opt_words(spec, args, i);
            continue;
        }
        if spec.subs.contains(&t) {
            return scan(spec, args, i + 1, spec.positionals, shell_mode, true);
        }
        if spec.shells.contains(&t) {
            return Run::Done(Outcome::Deny(SHELL_STDIN));
        }
        let value = std::mem::take(&mut maybe_value);
        if spec.groups.contains(&t) {
            i += 1;
            continue;
        }
        if spec.builtins.is_empty() || spec.builtins.contains(&t) {
            if !value {
                return Run::No;
            }
            // The value of an unlisted option; the subcommand can follow it.
            i += 1;
            continue;
        }
        let command = scan(spec, args, i, 0, shell_mode, true);
        if !value {
            return command;
        }
        let after = find_sub(spec, args, i + 1, shell_mode);
        return Run::Either(Box::new(command), Box::new(after));
    }
    Run::No
}

/// Scans runner arguments from `i` for the command. The `primary` reading forks at a word right
/// after an unlisted `--long` option; the second reading takes that word as the option's value.
/// Only the primary reading denies a missing command, because a flag before the command is
/// much more common than a valued option with nothing after it.
fn scan(
    spec: &Runner,
    args: &[Arg],
    mut i: usize,
    mut positionals: usize,
    mut shell_mode: bool,
    primary: bool,
) -> Run {
    let mut maybe_value = false;
    while let Some(t) = args.get(i).map(|a| a.text.as_str()) {
        if t == "--" || spec.rest.contains(&t) {
            return command_at(spec, args, i + 1, shell_mode);
        }
        if let Some(outcome) = shell_option(spec, args, i) {
            return Run::Done(outcome);
        }
        if let Some(value) = match_opt(t, &["-m", "--module"]).filter(|_| spec.python) {
            let (module, after) = match value {
                OptValue::Inline(v) => (Some(v), i + 1),
                OptValue::Next => (args.get(i + 1).map(|n| n.text.as_str()), i + 2),
            };
            let leftover = args.get(after..).unwrap_or(&[]);
            return Run::Done(module.map_or(Outcome::Allow, |m| python_module(m, leftover)));
        }
        if is_runner_opt(t) {
            shell_mode |= turns_on_shell_mode(spec, t);
            maybe_value = primary && unlisted_long(spec, t);
            i += opt_words(spec, args, i);
            continue;
        }
        // A hatch matrix word, or a word before a command option (`nix develop .#x -c`).
        if (spec.env_select && t.starts_with('+')) || spec.at == CommandAt::Option {
            i += 1;
        } else if positionals > 0 {
            positionals -= 1;
            i += 1;
        } else if let Some(j) = args[i..]
            .iter()
            .position(|w| w.text == "--")
            .filter(|_| spec.at == CommandAt::DashDash)
        {
            i += j;
        } else {
            let command = command_at(spec, args, i, shell_mode);
            if !maybe_value {
                return command;
            }
            let value = scan(spec, args, i + 1, 0, shell_mode, false);
            return Run::Either(Box::new(command), Box::new(value));
        }
        maybe_value = false;
    }
    match spec.at {
        CommandAt::Option => Run::Done(Outcome::Deny(SHELL_STDIN)),
        _ if spec.stdin_shell && primary => Run::Done(Outcome::Deny(SHELL_STDIN)),
        _ => Run::At(args.len()),
    }
}

/// The runner's command starts at `k`. A `-` command reads a script from stdin, shell mode runs
/// the words as shell source, and a runner with no command can start a shell.
fn command_at(spec: &Runner, args: &[Arg], k: usize, shell_mode: bool) -> Run {
    let words = args.get(k..).unwrap_or(&[]);
    let Some(first) = words.first() else {
        return if spec.stdin_shell {
            Run::Done(Outcome::Deny(SHELL_STDIN))
        } else {
            Run::At(args.len())
        };
    };
    if first.text == "-" || reads_stdin(first) {
        let rule = if spec.python {
            "python"
        } else {
            interpreter_family(spec.name).unwrap_or(SHELL_STDIN)
        };
        return Run::Done(Outcome::Deny(rule));
    }
    if !shell_mode {
        return Run::At(k);
    }
    let mut words = words.to_vec();
    if let Some((_, command)) = first.text.split_once(':').filter(|_| spec.env_select) {
        words[0].text = command.to_string();
    }
    Run::Done(reparse(&words))
}

// ---------------------------------------------------------------------------
// Option splitting

struct Parsed<'a> {
    flags: Vec<String>,
    operands: Vec<&'a Arg>,
}

impl Parsed<'_> {
    /// Reports whether the parsed options include the exact flag.
    fn has(&self, flag: &str) -> bool {
        self.flags.iter().any(|f| f == flag)
    }
    /// Reports whether any supplied flag appears in the parsed options.
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

/// Creates a reader specification with valued short options and no script operand.
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
            ("--files0-from", 1),
            ("--buffer-size", 1),
        ],
        ..reader("sort", "ktTSo")
    },
    Reader {
        valued_long: &[("--delimiters", 1)],
        ..reader("paste", "d")
    },
    reader("rev", ""),
    reader("unexpand", "t"),
    reader("fmt", "wpg"),
    reader("join", "12avejot"),
    reader("zcat", ""),
    Reader {
        valued_long: &[("--width", 1)],
        ..reader("fold", "w")
    },
    reader("pr", "hlwWNo"),
    reader("comm", ""),
    reader("column", "csoNTRHWL"),
    Reader {
        valued_long: &[("--tabs", 1)],
        ..reader("expand", "t")
    },
    Reader {
        valued_long: &[("--from-code", 1), ("--to-code", 1), ("--output", 1)],
        ..reader("iconv", "fto")
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

/// Denies file operands, `xargs` operands, and supported in-place reader modes.
fn read(spec: &Reader, args: &[Arg], xargs: bool) -> Outcome {
    let name = spec.name;
    let p = split(args, spec.valued_short, spec.valued_long);
    if matches!(name, "sed" | "gsed") && p.has_any(&["-i", "--in-place"]) {
        return Outcome::Deny(SED_I);
    }
    if name.ends_with("awk") && awk_inplace(args) {
        return Outcome::Deny(AWK_INPLACE);
    }
    if (name == "sort" && sort_writes(args))
        || (name == "jq" && jq_reads_file(args))
        || (name == "iconv" && output_to_file(args, "fto", "o", &["--output"]))
    {
        return Outcome::Deny(spec.name);
    }
    let mut scripts: Vec<&str> = Vec::new();
    if matches!(name, "sed" | "gsed") {
        scripts = flag_values(args, "efl", "e", &["--expression"]);
    } else if name.ends_with("awk") {
        scripts = flag_values(args, "Fvfei", "e", &["--source"]);
    }
    let mut operands = p.operands.iter().copied();
    if let Some(script_flags) = spec.script_flags {
        if !p.has_any(script_flags) {
            let first = operands.next();
            if let Some(program) = first {
                scripts.push(program.text.as_str());
            }
            let yq_sub = name == "yq"
                && first
                    .is_some_and(|a| matches!(a.text.as_str(), "e" | "ea" | "eval" | "eval-all"));
            if yq_sub {
                operands.next();
            }
        }
    }
    let io = if matches!(name, "sed" | "gsed") {
        scripts.iter().any(|s| sed_script_io(s))
    } else if name.ends_with("awk") {
        scripts.iter().any(|s| awk_program_io(s))
    } else {
        false
    };
    if io {
        return Outcome::Deny(spec.name);
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

/// Values of the options in `want_short` and `want_long` (long names may be abbreviated to
/// any prefix, as GNU getopt allows). `valued_short` lists every short flag of the command that takes a value.
fn flag_values<'a>(
    args: &'a [Arg],
    valued_short: &str,
    want_short: &str,
    want_long: &[&str],
) -> Vec<&'a str> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let t = args[i].text.as_str();
        i += 1;
        if t == "--" {
            break;
        }
        let next = args.get(i).map(|a| a.text.as_str());
        if let Some(long) = t.strip_prefix("--") {
            let (key, inline) = long
                .split_once('=')
                .map_or((long, None), |(k, v)| (k, Some(v)));
            let wanted = !key.is_empty()
                && want_long
                    .iter()
                    .any(|w| w.strip_prefix("--").is_some_and(|w| w.starts_with(key)));
            if wanted {
                out.extend(inline.or(next));
            }
        } else if let Some(cluster) = t.strip_prefix('-') {
            for (k, ch) in cluster.char_indices() {
                if !valued_short.contains(ch) {
                    continue;
                }
                let rest = &cluster[k + ch.len_utf8()..];
                if want_short.contains(ch) {
                    out.extend(if rest.is_empty() { next } else { Some(rest) });
                }
                if rest.is_empty() {
                    i += 1;
                }
                break;
            }
        }
    }
    out
}

/// An output option that names a file other than `/dev/null`.
fn output_to_file(args: &[Arg], valued_short: &str, short: &str, long: &[&str]) -> bool {
    flag_values(args, valued_short, short, long)
        .iter()
        .any(|v| *v != "/dev/null")
}

fn sort_writes(args: &[Arg]) -> bool {
    output_to_file(args, "ktTSo", "o", &["--output"])
        || !flag_values(args, "ktTSo", "", &["--files0-from"]).is_empty()
}

/// `jq --rawfile NAME FILE` and `--slurpfile NAME FILE` read FILE.
fn jq_reads_file(args: &[Arg]) -> bool {
    args.iter().enumerate().any(|(i, a)| {
        matches!(a.text.as_str(), "--rawfile" | "--slurpfile")
            && args.get(i + 2).is_some_and(|f| f.text != "/dev/null")
    })
}

/// A sed script with a command that reads or writes a file or runs a command:
/// `r`, `R`, `w`, `W`, `e`, and the `w` and `e` flags of `s`.
fn sed_script_io(script: &str) -> bool {
    let c: Vec<char> = script.chars().collect();
    let skip_to = |mut i: usize, delim: char| {
        while i < c.len() {
            if c[i] == '\\' {
                i += 2;
            } else if c[i] == delim {
                return i + 1;
            } else {
                i += 1;
            }
        }
        c.len()
    };
    let mut i = 0;
    while i < c.len() {
        while i < c.len() && (c[i].is_whitespace() || matches!(c[i], ';' | '{' | '}')) {
            i += 1;
        }
        loop {
            match c.get(i) {
                Some('/') => i = skip_to(i + 1, '/'),
                Some('\\') => match c.get(i + 1) {
                    Some(d) => i = skip_to(i + 2, *d),
                    None => return false,
                },
                Some(ch)
                    if ch.is_ascii_digit()
                        || ch.is_whitespace()
                        || matches!(ch, '$' | ',' | '~' | '+' | '!' | 'I' | 'M') =>
                {
                    i += 1;
                }
                _ => break,
            }
        }
        let Some(&cmd) = c.get(i) else { break };
        i += 1;
        match cmd {
            'r' | 'R' | 'w' | 'W' | 'e' => return true,
            's' | 'y' => {
                let Some(&d) = c.get(i) else { break };
                i = skip_to(skip_to(i + 1, d), d);
                while cmd == 's' && i < c.len() && !matches!(c[i], ';' | '\n' | '}') {
                    if matches!(c[i], 'w' | 'e') {
                        return true;
                    }
                    i += 1;
                }
            }
            'a' | 'i' | 'c' => {
                while i < c.len() && c[i] != '\n' {
                    i += 1;
                }
            }
            ':' | 'b' | 't' | 'T' => {
                while i < c.len() && !matches!(c[i], ';' | '\n') {
                    i += 1;
                }
            }
            _ => {}
        }
    }
    false
}

/// An awk program that reads a file (`getline`), runs a command (`system`), or redirects or
/// pipes `print` output.
fn awk_program_io(prog: &str) -> bool {
    if prog.contains("getline")
        || prog.contains("ARGV[")
        || prog
            .split("system")
            .skip(1)
            .any(|rest| rest.trim_start().starts_with('('))
    {
        return true;
    }
    prog.match_indices("print").any(|(at, word)| {
        let (mut depth, mut quoted, mut escaped) = (0usize, false, false);
        for ch in prog[at + word.len()..].chars() {
            if quoted {
                match (escaped, ch) {
                    (false, '\\') => escaped = true,
                    (false, '"') => quoted = false,
                    _ => escaped = false,
                }
                continue;
            }
            match ch {
                '"' => quoted = true,
                '(' => depth += 1,
                ')' => depth = depth.saturating_sub(1),
                ';' | '}' | '\n' if depth == 0 => return false,
                '>' | '|' if depth == 0 => return true,
                _ => {}
            }
        }
        false
    })
}

/// Recognizes assignments whose names are ASCII shell identifiers.
fn is_assignment(text: &str) -> bool {
    text.split_once('=').is_some_and(|(k, _)| {
        !k.is_empty()
            && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            && !k.starts_with(|c: char| c.is_ascii_digit())
    })
}

/// Detects the attached or separate `-i inplace` awk option.
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

/// Returns the option and recursion policy for a recognized search command.
fn searcher(name: &str) -> Option<&'static Searcher> {
    match name {
        "grep" | "egrep" | "fgrep" | "ugrep" | "ug" | "zgrep" | "zegrep" | "zfgrep" => Some(&GREP),
        "rg" => Some(&RG),
        "ag" => Some(&AG),
        "ack" => Some(&ACK),
        _ => None,
    }
}

/// Returns the static denial rule for a recognized search command.
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

/// `ast-grep` and `sg` search the working tree unless `--stdin` feeds them; `-U` rewrites files.
fn ast_grep(args: &[Arg], cmd: &Cmd, xargs: bool) -> Outcome {
    if args
        .first()
        .is_some_and(|a| matches!(a.text.as_str(), "--version" | "--help" | "-V" | "-h"))
    {
        return Outcome::Allow;
    }
    let updates = args.iter().any(|a| {
        a.text == "--update-all"
            || (a.text.starts_with('-') && !a.text.starts_with("--") && a.text.contains('U'))
    });
    let stdin_fed = (cmd.piped || cmd.here) && args.iter().any(|a| a.text == "--stdin");
    if updates || xargs || !stdin_fed {
        return Outcome::Deny("rg");
    }
    Outcome::Allow
}

/// Checks whether `text` abbreviates `word` with at least `min` bytes.
fn is_prefix_of(word: &str, text: &str, min: usize) -> bool {
    text.len() >= min && word.starts_with(text)
}

/// Recognizes nonempty, case-sensitive abbreviations of `recurse`.
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

/// Allows listing and probe modes, then denies file operands or implicit repo searches.
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

/// `tee` and `sponge` write their operands; `xargs` feeds them operands too.
/// Denies output operands other than `-` and `/dev/null`.
fn writer(args: &[Arg], xargs: bool, rule: Rule) -> Outcome {
    let p = split(args, "", &[]);
    if xargs
        || p.operands
            .iter()
            .any(|a| a.text != "-" && a.text != "/dev/null")
    {
        Outcome::Deny(rule)
    } else {
        Outcome::Allow
    }
}

/// A text editor in a tool call runs scripted edits (`vim -es`, `nvim --headless`).
fn vim(args: &[Arg]) -> Outcome {
    match args.first().map(|a| a.text.as_str()) {
        Some("--version" | "--help") => Outcome::Allow,
        _ => Outcome::Deny("vim"),
    }
}

/// `fd -x` and `fd -X` run a command per match, like `find -exec`.
fn fd(args: &[Arg]) -> Outcome {
    let runs = args.iter().take_while(|a| a.text != "--").any(|a| {
        let t = a.text.as_str();
        match t.strip_prefix("--") {
            Some(long) => matches!(long.split('=').next(), Some("exec" | "exec-batch")),
            None => t.starts_with('-') && t.contains(['x', 'X']),
        }
    });
    if runs {
        Outcome::Deny("find -exec")
    } else {
        Outcome::Allow
    }
}

/// `trap CODE SIGNAL...` runs CODE as shell source.
fn trap(args: &[Arg]) -> Outcome {
    let p = split(args, "", &[]);
    match p.operands.as_slice() {
        [code, _, ..] if code.text != "-" => Outcome::Reparse(code.text.clone()),
        _ => Outcome::Allow,
    }
}

/// `script` writes a typescript file and `-c` runs a shell command. Only `/dev/null` is a safe
/// typescript.
fn script(args: &[Arg]) -> Outcome {
    let valued = "cIOBETmo";
    let p = split(args, valued, &[("--command", 1)]);
    let log = output_to_file(
        args,
        valued,
        "IOBT",
        &["--log-in", "--log-out", "--log-io", "--log-timing"],
    );
    if log || p.operands.is_empty() || p.operands.iter().any(|a| a.text != "/dev/null") {
        return Outcome::Deny(OUTPUT_REDIRECT);
    }
    match flag_values(args, valued, "c", &["--command"]).first() {
        Some(command) => Outcome::Reparse((*command).to_string()),
        None => Outcome::Allow,
    }
}

/// `curl` reads `file://` URLs and `@file` data, and writes with `-o`, `-O`, and similar.
fn curl(args: &[Arg]) -> Outcome {
    let harmless = |v: Option<&str>| v.is_some_and(|v| matches!(v, "/dev/null" | "-"));
    for (i, a) in args.iter().enumerate() {
        let t = a.text.as_str();
        let next = args.get(i + 1).map(|n| n.text.as_str());
        let file_data = (t.starts_with('@') && t != "@-")
            || (!t.contains("://") && (t.contains("=@") || t.contains("=<")));
        if t.to_ascii_lowercase().contains("file://") || file_data {
            return Outcome::Deny(INPUT_REDIRECT);
        }
        if let Some(long) = t.strip_prefix("--") {
            let (key, inline) = long
                .split_once('=')
                .map_or((long, None), |(k, v)| (k, Some(v)));
            let value = inline.or(next);
            match key {
                "output" | "dump-header" | "cookie-jar" | "trace" | "trace-ascii" | "stderr"
                    if !harmless(value) =>
                {
                    return Outcome::Deny(OUTPUT_REDIRECT);
                }
                "remote-name" | "remote-name-all" => return Outcome::Deny(OUTPUT_REDIRECT),
                "upload-file" | "config" if !harmless(value) => {
                    return Outcome::Deny(INPUT_REDIRECT);
                }
                _ => {}
            }
        } else if let Some(cluster) = t.strip_prefix('-') {
            for (k, ch) in cluster.char_indices() {
                let rest = &cluster[k + ch.len_utf8()..];
                let value = if rest.is_empty() { next } else { Some(rest) };
                if (rest.starts_with('@') && rest != "@-") || rest.starts_with('<') {
                    return Outcome::Deny(INPUT_REDIRECT);
                }
                match ch {
                    'O' => return Outcome::Deny(OUTPUT_REDIRECT),
                    'o' | 'D' | 'c' if !harmless(value) => return Outcome::Deny(OUTPUT_REDIRECT),
                    'T' | 'K' if !harmless(value) => return Outcome::Deny(INPUT_REDIRECT),
                    'o' | 'D' | 'c' | 'T' | 'K' => break,
                    c if "ABCEFHPQUXYZbdehmrtuwxyz".contains(c) => break,
                    _ => {}
                }
            }
        }
    }
    Outcome::Allow
}

/// `tar -xO` and `tar --to-stdout` print archive members.
fn tar(args: &[Arg]) -> Outcome {
    if tar_runs_command(args) || tar_creates_to_stdout(args) {
        return Outcome::Deny("tar");
    }
    let to_stdout = args.iter().enumerate().any(|(i, a)| {
        let t = a.text.as_str();
        t == "--to-stdout"
            || (t.starts_with('-') && !t.starts_with("--") && t.contains('O'))
            || (i == 0 && t.chars().all(|c| c.is_ascii_alphabetic()) && t.contains('O'))
    });
    if to_stdout {
        Outcome::Deny("tar")
    } else {
        Outcome::Allow
    }
}

/// `--to-command` (or a prefix of it) pipes each member into a command.
fn tar_runs_command(args: &[Arg]) -> bool {
    args.iter().any(|a| {
        let key = a
            .text
            .strip_prefix("--")
            .map(|l| l.split('=').next().unwrap_or(l));
        key.is_some_and(|k| k.len() >= 4 && "to-command".starts_with(k))
    })
}

/// `tar -c` with no `-f`, or `-f -`, writes the archive to stdout.
fn tar_creates_to_stdout(args: &[Arg]) -> bool {
    let mut create = false;
    let mut file: Option<&str> = None;
    for (i, a) in args.iter().enumerate() {
        let t = a.text.as_str();
        let next = args.get(i + 1).map(|n| n.text.as_str());
        if t == "--create" {
            create = true;
        } else if let Some(v) = t.strip_prefix("--file=") {
            file = Some(v);
        } else if t == "--file" {
            file = next;
        } else if !t.starts_with("--") && (t.starts_with('-') || i == 0) {
            let cluster = t.strip_prefix('-').unwrap_or(t);
            if i == 0 && !cluster.chars().all(|c| c.is_ascii_alphabetic()) {
                continue;
            }
            create |= cluster.contains('c');
            if let Some(at) = cluster.find('f') {
                let rest = &cluster[at + 1..];
                file = if rest.is_empty() { next } else { Some(rest) };
            }
        }
    }
    create && file.is_none_or(|f| f == "-")
}

/// Denies `find` actions that execute commands, including interactive variants.
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

/// Skips global Git options and denies content reads, searches, and patch application.
fn git(args: &[Arg]) -> Outcome {
    let mut i = 0;
    while let Some(arg) = args.get(i) {
        let t = arg.text.as_str();
        if !t.starts_with('-') {
            break;
        }
        if t == "-c" && args.get(i + 1).is_some_and(|v| runs_command(&v.text)) {
            return Outcome::Deny(GIT_CONFIG);
        }
        let config_env = t.strip_prefix("--config-env").and_then(|v| match v {
            "" => args.get(i + 1).map(|a| a.text.as_str()),
            _ => v.strip_prefix('='),
        });
        if config_env.is_some_and(runs_command) {
            return Outcome::Deny(GIT_CONFIG);
        }
        let separate = matches!(
            t,
            "-C" | "-c" | "--git-dir" | "--work-tree" | "--namespace" | "--config-env"
        );
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
        "diff" | "difftool"
            if rest.iter().any(|a| {
                (a.text.starts_with("--no-i") && "--no-index".starts_with(a.text.as_str()))
                    || a.text.starts_with('/')
            }) =>
        {
            Outcome::Deny("diff")
        }
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

/// A leading `NAME=value` environment assignment that makes git run a command.
fn git_env_runs_command(assign: &str) -> bool {
    let Some((name, value)) = assign.split_once('=') else {
        return false;
    };
    match name {
        "GIT_CONFIG_PARAMETERS"
        | "GIT_EXTERNAL_DIFF"
        | "GIT_SSH_COMMAND"
        | "GIT_SSH"
        | "GIT_EDITOR"
        | "GIT_SEQUENCE_EDITOR"
        | "GIT_ASKPASS" => true,
        "GIT_PAGER" | "PAGER" => !matches!(value, "cat" | "less"),
        _ => name.starts_with("GIT_CONFIG_KEY_"),
    }
}

/// A `git -c name=value` setting that makes git run a command.
fn runs_command(setting: &str) -> bool {
    let key = setting
        .split('=')
        .next()
        .unwrap_or(setting)
        .to_ascii_lowercase();
    key.starts_with("alias.")
        || key.starts_with("pager.")
        || key.ends_with(".textconv")
        || (key.starts_with("credential.") && key.ends_with(".helper"))
        || (key.starts_with("gpg.") && key.ends_with(".program"))
        || (key.starts_with("diff.") && key.ends_with(".command"))
        || (key.starts_with("filter.")
            && [".clean", ".smudge", ".process"]
                .iter()
                .any(|s| key.ends_with(s)))
        || matches!(
            key.as_str(),
            "core.pager"
                | "credential.helper"
                | "gpg.program"
                | "core.hookspath"
                | "core.editor"
                | "core.sshcommand"
                | "core.fsmonitor"
                | "core.askpass"
                | "diff.external"
                | "sequence.editor"
        )
}

// ---------------------------------------------------------------------------
// Shells

fn shell(args: &[Arg], fish: bool) -> Outcome {
    let mut init: Option<Arg> = None;
    let mut i = 0;
    while let Some(arg) = args.get(i) {
        if arg.text == "--" {
            break;
        }
        if fish && matches!(arg.text.as_str(), "-C" | "--init-command") {
            init = args.get(i + 1).map(|a| Arg {
                text: a.text.clone(),
                literal: a.literal,
                opaque: a.opaque,
            });
        } else if let Some(v) = arg.text.strip_prefix("--init-command=").filter(|_| fish) {
            init = Some(Arg {
                text: v.to_string(),
                literal: arg.literal,
                opaque: arg.opaque,
            });
        }
        i += 1;
    }
    let outcome = shell_operand(args, fish);
    let Some(command) = init else {
        return outcome;
    };
    match outcome {
        Outcome::Allow => reparse(std::slice::from_ref(&command)),
        Outcome::Reparse(text) => match reparse(std::slice::from_ref(&command)) {
            Outcome::Reparse(first) => Outcome::Reparse(format!("{first}\n{text}")),
            other => other,
        },
        Outcome::Deny(rule) => Outcome::Deny(rule),
    }
}

/// Reparses `-c` source and denies stdin scripts or unresolved script paths.
fn shell_operand(args: &[Arg], fish: bool) -> Outcome {
    let mut c_mode = false;
    let mut informational = false;
    let mut stdin_flag = false;
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
        } else if t == "--command" {
            c_mode = true;
        } else if (fish && matches!(t, "-C" | "--init-command"))
            || matches!(t, "--rcfile" | "--init-file" | "-o" | "+o" | "-O" | "+O")
        {
            i += 1;
        } else if t.len() > 1 && (t.starts_with('-') || t.starts_with('+')) {
            c_mode |= t.starts_with('-') && !t.starts_with("--") && t.contains('c');
            stdin_flag |= t.starts_with('-') && !t.starts_with("--") && t.contains('s');
        } else {
            operand = Some(i - 1);
            break;
        }
    }
    match operand {
        Some(k) if c_mode => reparse(&args[k..=k]),
        Some(k) if stdin_flag || reads_stdin(&args[k]) => Outcome::Deny(SHELL_STDIN),
        Some(k) if !args[k].literal || args[k].opaque => Outcome::Deny(UNRESOLVED),
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

/// Maps interpreter names, including numeric version suffixes, to their rule family.
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

/// Returns the inline-code and valued-option policy for an interpreter family.
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

/// Checks interpreter arguments for inline code, stdin scripts, and file-reading JSON tools.
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
        return python_module(&module, args.get(module_end..).unwrap_or(&[]));
    }
    if let Some(s) = script {
        let word = Arg {
            text: s.to_string(),
            literal: true,
            opaque: false,
        };
        if s == "-" || reads_stdin(&word) {
            return Outcome::Deny(family);
        }
    }
    if matches!(family, "deno" | "bun") {
        let sub_at = args.iter().position(|a| Some(a.text.as_str()) == script);
        let target = sub_at.and_then(|at| {
            let mut rest = args[at + 1..].iter();
            while let Some(a) = rest.next() {
                if a.text == "-" || !a.text.starts_with('-') {
                    return Some(a);
                }
                if DENO_VALUED.contains(&a.text.as_str()) {
                    rest.next();
                }
            }
            None
        });
        match (family, script) {
            ("bun", Some("exec")) => return Outcome::Deny(family),
            (_, Some("run")) if target.is_some_and(|a| a.text == "-" || reads_stdin(a)) => {
                return Outcome::Deny(family);
            }
            _ => {}
        }
    }
    match (family, script) {
        ("deno", Some("eval")) => Outcome::Deny(family),
        ("deno" | "bun", _) | (_, Some(_)) => Outcome::Allow,
        _ if informational => Outcome::Allow,
        _ => Outcome::Deny(family),
    }
}

/// `deno` and `bun` flags that take the next argument as their value.
const DENO_VALUED: &[&str] = &[
    "--config",
    "-c",
    "--import-map",
    "--cert",
    "--location",
    "--seed",
    "--v8-flags",
    "--ext",
];

/// The word names stdin or a process substitution, so the shell reads a script nobody can see.
fn reads_stdin(arg: &Arg) -> bool {
    let path = normalize_path(&arg.text);
    matches!(path.as_str(), "/dev/stdin" | "/proc/self/fd/0")
        || path.starts_with("/dev/fd/")
        || path.starts_with("/proc/self/fd/")
        || arg.text.starts_with("<(")
}

/// `source` and `.` run a file in the current shell; deny a script that is not a literal path.
fn source(args: &[Arg]) -> Outcome {
    let operand = args.iter().find(|a| a.text != "--");
    match operand {
        Some(a) if a.text == "-" || reads_stdin(a) => Outcome::Deny(SHELL_STDIN),
        Some(a) if !a.literal || a.opaque => Outcome::Deny(UNRESOLVED),
        _ => Outcome::Allow,
    }
}

/// Checks `python -m MODULE`: a module that reads its file operands or starts a REPL denies.
fn python_module(module: &str, leftover: &[Arg]) -> Outcome {
    let reads_file = leftover.iter().any(|a| !a.text.starts_with('-'));
    match module {
        "json.tool" if reads_file => Outcome::Deny(JSON_TOOL),
        "base64" | "tokenize" | "ast" | "dis" | "pickle" | "pickletools" | "gzip" | "quopri"
        | "uu" | "zipfile" | "tarfile" | "calendar" | "cProfile" | "profile" | "trace"
        | "runpy" | "pydoc"
            if reads_file =>
        {
            Outcome::Deny(PYTHON_M)
        }
        "code" | "pdb" | "sqlite3" | "timeit" | "idlelib" | "turtledemo" => Outcome::Deny("python"),
        _ => Outcome::Allow,
    }
}

// ---------------------------------------------------------------------------
// Programs that read commands from stdin

/// Flags that print version or help text and exit.
const INFO_FLAGS: &[&str] = &[
    "--version",
    "-version",
    "-v",
    "-V",
    "--help",
    "-help",
    "-h",
    "-?",
    "--numeric-version",
    "--info",
    "--configuration",
];

/// Routes a command that is not an inline interpreter to the debugger, make, sqlite3, or stdin
/// REPL rules. `env` holds the `NAME=value` words that set the command's environment.
fn stdin_reader(name: &str, args: &[Arg], env: &[&Arg]) -> Outcome {
    let stem = name.trim_end_matches(|c: char| c.is_ascii_digit() || matches!(c, '.' | '-'));
    match stem {
        "gdb" | "rust-gdb" | "gdb-multiarch" => debugger(&GDB, args),
        "lldb" | "rust-lldb" => debugger(&LLDB, args),
        "make" | "gmake" if make_evals(args) || env.iter().any(|a| make_runs_code(&a.text)) => {
            Outcome::Deny(MAKE_EVAL)
        }
        "sqlite" => {
            let args = sqlite_args(args);
            if sqlite_io(&args) {
                Outcome::Deny(SQLITE_IO)
            } else {
                stdin_repl(stem, &args)
            }
        }
        _ => stdin_repl(stem, args),
    }
}

/// Checks a program from `REPLS`. Any other program is allowed.
fn stdin_repl(stem: &str, args: &[Arg]) -> Outcome {
    REPLS
        .iter()
        .find(|r| r.name == stem)
        .map_or(Outcome::Allow, |spec| repl_program(spec, args))
}

/// What a gdb or lldb option does.
#[derive(Clone, Copy, PartialEq, Eq)]
enum DebugOpt {
    /// A flag that does not change what the rules check.
    Flag,
    /// Quit after the command options run, instead of reading commands from stdin.
    Batch,
    /// Print version or help text and exit.
    Info,
    /// gdb `--args`: the remaining words are the program and its arguments.
    Args,
    /// The value is one debugger command.
    Command,
    /// The value is a file of debugger commands.
    File,
    /// The value is the program to debug.
    Program,
    /// gdb `-tty`: the value is the terminal that the program reads and writes.
    Tty,
    /// Another option that takes a value.
    Valued,
    /// lldb `--repl`: a language REPL that reads stdin.
    Repl,
}

impl DebugOpt {
    /// The option takes a value: the text after `=`, or the next word.
    fn takes_value(self) -> bool {
        matches!(
            self,
            Self::Command | Self::File | Self::Program | Self::Tty | Self::Valued
        )
    }
}

/// The command-line options of gdb or lldb.
struct Debugger {
    /// Every option name without its dashes, and what the option does.
    options: &'static [(&'static str, DebugOpt)],
    /// gdb: the words after `--args` are the program and its arguments, and the first operand
    /// is the program. lldb: the operands and the words after `--` are.
    gdb: bool,
}

/// The `long_options` table of gdb 17 (`gdb/main.c`), and `-dbx` from older releases. The
/// rules resolve a unique prefix against this whole table, so it must list every option.
const GDB: Debugger = Debugger {
    options: &[
        ("annotate", DebugOpt::Valued),
        ("args", DebugOpt::Args),
        ("b", DebugOpt::Valued),
        ("batch", DebugOpt::Batch),
        ("batch-silent", DebugOpt::Batch),
        ("baud", DebugOpt::Valued),
        ("c", DebugOpt::Valued),
        ("cd", DebugOpt::Valued),
        ("command", DebugOpt::File),
        ("configuration", DebugOpt::Info),
        ("core", DebugOpt::Valued),
        ("D", DebugOpt::Valued),
        ("d", DebugOpt::Valued),
        ("data-directory", DebugOpt::Valued),
        ("dbx", DebugOpt::Flag),
        ("directory", DebugOpt::Valued),
        ("e", DebugOpt::Program),
        ("early-init-command", DebugOpt::File),
        ("early-init-eval-command", DebugOpt::Command),
        ("eiex", DebugOpt::Command),
        ("eix", DebugOpt::File),
        ("eval-command", DebugOpt::Command),
        ("ex", DebugOpt::Command),
        ("exec", DebugOpt::Program),
        ("f", DebugOpt::Flag),
        ("fullname", DebugOpt::Flag),
        ("help", DebugOpt::Info),
        ("i", DebugOpt::Valued),
        ("iex", DebugOpt::Command),
        ("init-command", DebugOpt::File),
        ("init-eval-command", DebugOpt::Command),
        ("interpreter", DebugOpt::Valued),
        ("ix", DebugOpt::File),
        ("l", DebugOpt::Valued),
        ("n", DebugOpt::Flag),
        ("nh", DebugOpt::Flag),
        ("nowindows", DebugOpt::Flag),
        ("nw", DebugOpt::Flag),
        ("nx", DebugOpt::Flag),
        ("p", DebugOpt::Valued),
        ("pid", DebugOpt::Valued),
        ("q", DebugOpt::Flag),
        ("quiet", DebugOpt::Flag),
        ("r", DebugOpt::Flag),
        ("readnever", DebugOpt::Flag),
        ("readnow", DebugOpt::Flag),
        ("return-child-result", DebugOpt::Flag),
        ("s", DebugOpt::Valued),
        ("se", DebugOpt::Program),
        ("silent", DebugOpt::Flag),
        ("statistics", DebugOpt::Flag),
        ("symbols", DebugOpt::Valued),
        ("tty", DebugOpt::Tty),
        ("tui", DebugOpt::Flag),
        ("ui", DebugOpt::Valued),
        ("version", DebugOpt::Info),
        ("w", DebugOpt::Flag),
        ("windows", DebugOpt::Flag),
        ("write", DebugOpt::Flag),
        ("x", DebugOpt::File),
    ],
    gdb: true,
};

/// The lldb options that change what the rules check (`lldb/tools/driver/Options.td`). lldb
/// ignores other options.
const LLDB: Debugger = Debugger {
    options: &[
        ("o", DebugOpt::Command),
        ("one-line", DebugOpt::Command),
        ("O", DebugOpt::Command),
        ("one-line-before-file", DebugOpt::Command),
        ("k", DebugOpt::Command),
        ("one-line-on-crash", DebugOpt::Command),
        ("s", DebugOpt::File),
        ("source", DebugOpt::File),
        ("S", DebugOpt::File),
        ("source-before-file", DebugOpt::File),
        ("K", DebugOpt::File),
        ("source-on-crash", DebugOpt::File),
        ("b", DebugOpt::Batch),
        ("batch", DebugOpt::Batch),
        ("f", DebugOpt::Program),
        ("file", DebugOpt::Program),
        ("c", DebugOpt::Valued),
        ("core", DebugOpt::Valued),
        ("p", DebugOpt::Valued),
        ("attach-pid", DebugOpt::Valued),
        ("n", DebugOpt::Valued),
        ("attach-name", DebugOpt::Valued),
        ("a", DebugOpt::Valued),
        ("arch", DebugOpt::Valued),
        ("l", DebugOpt::Valued),
        ("script-language", DebugOpt::Valued),
        ("R", DebugOpt::Valued),
        ("repl-language", DebugOpt::Valued),
        ("v", DebugOpt::Info),
        ("version", DebugOpt::Info),
        ("h", DebugOpt::Info),
        ("help", DebugOpt::Info),
        ("r", DebugOpt::Repl),
        ("repl", DebugOpt::Repl),
    ],
    gdb: false,
};

/// Resolves a debugger option word to what it does and its inline value. `None` is an option
/// that gdb rejects (unknown, ambiguous, or a flag with a value) or that lldb ignores.
///
/// gdb reads options with `getopt_long_only`: one or two dashes, an exact name or a unique
/// prefix of one, and `NAME=VALUE`. lldb reads exact names: a long name takes one or two
/// dashes, and a one-letter name takes one dash. lldb does not read `--NAME=VALUE` or
/// `-xVALUE`, but the rules read them for an option with a value, so they check more.
fn debugger_option<'t>(spec: &Debugger, t: &'t str) -> Option<(DebugOpt, Option<&'t str>)> {
    let (double, word) = match t.strip_prefix("--") {
        Some(word) => (true, word),
        None => (false, &t[1..]),
    };
    if spec.gdb {
        let (name, value) = match word.split_once('=') {
            Some((name, value)) => (name, Some(value)),
            None => (word, None),
        };
        let mut prefixed = spec.options.iter().filter(|(n, _)| n.starts_with(name));
        let found = spec.options.iter().find(|(n, _)| *n == name).or_else(|| {
            let only = prefixed.next()?;
            prefixed.next().is_none().then_some(only)
        });
        return found
            .filter(|(_, kind)| value.is_none() || kind.takes_value())
            .map(|&(_, kind)| (kind, value));
    }
    let exact = spec
        .options
        .iter()
        .find(|(n, _)| *n == word && (n.len() > 1 || !double));
    if let Some(&(_, kind)) = exact {
        return Some((kind, None));
    }
    let (long, short): (Vec<_>, Vec<_>) = spec.options.iter().partition(|(n, _)| n.len() > 1);
    long.into_iter().chain(short).find_map(|&(n, kind)| {
        let value = if n.len() > 1 {
            word.strip_prefix(n)?.strip_prefix('=')?
        } else if double {
            return None;
        } else {
            word.strip_prefix(n)?
        };
        (kind.takes_value() || kind == DebugOpt::Repl).then_some((kind, Some(value)))
    })
}

/// gdb and lldb read commands from stdin unless `-batch` stops them. A command option can run a
/// shell command, which the rules reparse, or a script or meta command, which denies. The rules
/// check the program and its arguments like any other command.
fn debugger(spec: &Debugger, args: &[Arg]) -> Outcome {
    let mut scripts: Vec<String> = Vec::new();
    let mut program: Vec<Arg> = Vec::new();
    let (mut batched, mut info) = (false, false);
    let mut i = 0;
    while let Some(a) = args.get(i) {
        let t = a.text.as_str();
        i += 1;
        if t == "--" {
            let rest = &args[i..];
            let take = if spec.gdb && program.is_empty() {
                rest.len().min(1)
            } else if spec.gdb {
                0
            } else {
                rest.len()
            };
            program.extend_from_slice(&rest[..take]);
            break;
        }
        if t == "-" || !t.starts_with('-') {
            // gdb reads a core file or pid after the program.
            if !spec.gdb || program.is_empty() {
                program.push(a.clone());
            }
            continue;
        }
        let Some((kind, inline)) = debugger_option(spec, t) else {
            if spec.gdb {
                // gdb exits on this option, but the rules cannot tell which one the word is.
                return Outcome::Deny(UNRESOLVED);
            }
            continue;
        };
        let value = match inline {
            Some(v) => Some(Arg {
                text: v.to_string(),
                literal: a.literal,
                opaque: a.opaque,
            }),
            None if kind.takes_value() => {
                i += 1;
                args.get(i - 1).cloned()
            }
            None => None,
        };
        match kind {
            DebugOpt::Flag | DebugOpt::Valued => {}
            DebugOpt::Batch => batched = true,
            DebugOpt::Info => info = true,
            DebugOpt::Repl => return Outcome::Deny(STDIN_REPL),
            DebugOpt::Tty => return Outcome::Deny(DEBUGGER_SCRIPT),
            DebugOpt::Args => {
                program = args[i..].to_vec();
                break;
            }
            DebugOpt::Program => {
                program.splice(0..0, value);
            }
            DebugOpt::File => {
                if value.is_some_and(|v| v.text == "-") {
                    return Outcome::Deny(STDIN_REPL);
                }
            }
            DebugOpt::Command => {
                let Some(value) = value else { break };
                if value.opaque {
                    return Outcome::Deny(UNRESOLVED);
                }
                match debugger_command(&value.text) {
                    Ok(Some(source)) => scripts.push(source),
                    Ok(None) => {}
                    Err(rule) => return Outcome::Deny(rule),
                }
            }
        }
    }
    if !batched && !info {
        return Outcome::Deny(STDIN_REPL);
    }
    let scripts = if scripts.is_empty() {
        Outcome::Allow
    } else {
        Outcome::Reparse(scripts.join("\n"))
    };
    if program.is_empty() {
        return scripts;
    }
    let run = Cmd {
        words: &program,
        assigns: &[],
        piped: false,
        here: false,
    };
    either(check(&run), scripts)
}

/// Debugger commands that run a script or a shell, define or rename commands, read a command
/// body from stdin, or run another command (`eval`, `with`, `make`). Each entry is a name and
/// the shortest abbreviation that the rules match.
const DEBUGGER_DENY: &[(&str, usize)] = &[
    ("pipe", 3),
    ("python", 3),
    ("python-interactive", 3),
    ("guile", 3),
    ("script", 3),
    ("eval", 3),
    ("alias", 2),
    ("with", 1),
    ("make", 3),
    ("define", 3),
    ("define-prefix", 3),
    ("document", 3),
    ("source", 2),
    ("if", 2),
    ("while", 3),
    ("commands", 3),
    ("compile", 3),
    ("interpreter-exec", 5),
    // lldb: `command alias`, `command regex`, `command script`, `command source`.
    ("command", 3),
];

/// Classifies one gdb or lldb command. `Ok(Some(source))` is a shell command to reparse.
///
/// The rules deny known meta commands instead of allowing only known-safe ones. A deny list is
/// simpler, and it keeps every plain inspection command (`bt`, `info`, `frame`, `print`) allowed.
fn debugger_command(text: &str) -> Result<Option<String>, Rule> {
    let text = text.trim_start();
    // `$_shell(CMD)` runs CMD inside any expression (`print $_shell("cat f")`).
    if text.contains("$_shell") {
        return Err(DEBUGGER_SCRIPT);
    }
    let (head, rest) = if let Some(rest) = text.strip_prefix('!') {
        ("shell", rest)
    } else if let Some(rest) = text.strip_prefix('|') {
        ("pipe", rest)
    } else {
        first_word(text)
    };
    let (head, rest) = match first_word(rest) {
        (sub, after) if abbrev(head, "platform", 3) && abbrev(sub, "shell", 2) => ("shell", after),
        _ if abbrev(head, "platform", 3) => return Ok(None),
        _ => (head, rest),
    };
    let rest = rest.trim();
    if abbrev(head, "shell", 2) {
        return match rest {
            "" => Err(SHELL_STDIN),
            r if r.starts_with('-') => Err(DEBUGGER_SCRIPT),
            r => Ok(Some(r.to_string())),
        };
    }
    let (sub, after) = first_word(rest);
    // `thread apply all bt` and `taas bt` run another command on each thread or frame.
    if matches!(head, "taas" | "faas" | "tfaas") {
        return debugger_command(apply_body(rest));
    }
    if (abbrev(head, "thread", 1) || abbrev(head, "frame", 1)) && abbrev(sub, "apply", 1) {
        return debugger_command(apply_body(after));
    }
    let denied = matches!(head, "py" | "pi" | "gu")
        || DEBUGGER_DENY
            .iter()
            .any(|&(full, min)| abbrev(head, full, min))
        || sets_program_input(head, sub, after, rest)
        || text.contains("run-args")
        || text.contains("input-path");
    if denied {
        Err(DEBUGGER_SCRIPT)
    } else {
        Ok(None)
    }
}

/// The command after a thread or frame list: `all`, `level`, IDs, ranges, `$var` IDs, and
/// flags.
fn apply_body(text: &str) -> &str {
    let mut rest = text;
    loop {
        let (word, after) = first_word(rest);
        let list = abbrev(word, "all", 1)
            || word == "level"
            || word.starts_with(|c: char| c == '-' || c == '$' || c.is_ascii_digit());
        if !list {
            return rest;
        }
        rest = after;
    }
}

/// The command gives the program arguments or input. `run ARGS` passes them through a shell,
/// and the rules check program arguments only after `--args` (gdb) or `--` (lldb).
fn sets_program_input(head: &str, sub: &str, after: &str, rest: &str) -> bool {
    let runs = abbrev(head, "run", 1) || matches!(head, "start" | "starti");
    let launches = abbrev(head, "process", 3) && abbrev(sub, "launch", 1);
    (runs && !rest.is_empty())
        || (launches && !after.trim().is_empty())
        || (head == "set" && (abbrev(sub, "args", 2) || abbrev(sub, "inferior-tty", 3)))
        || head == "tty"
}

/// Splits off the first whitespace-separated word.
fn first_word(text: &str) -> (&str, &str) {
    let text = text.trim_start();
    text.split_once(char::is_whitespace).unwrap_or((text, ""))
}

/// The word is `full` or an abbreviation of it with at least `min` characters.
fn abbrev(word: &str, full: &str, min: usize) -> bool {
    word.len() >= min && full.starts_with(word)
}

/// A program that reads commands from stdin unless an operand or an option names its program.
struct Repl {
    /// The command name without version digits: `sqlite3` is `sqlite`, `lua5.4` is `lua`.
    name: &'static str,
    /// Operands up to and including the program; `sqlite3 DB SQL` needs two. Zero means no
    /// operand names the program: `ghci FILE` loads the file and then reads stdin.
    operands: usize,
    /// Options that take a value.
    valued: &'static [&'static str],
    /// Options, and bare words such as `R CMD`, that name the program. A `-` value is stdin.
    program: &'static [&'static str],
    /// Flags that start the REPL even after a program runs (`lua -i`).
    interactive: &'static [&'static str],
    /// The program has a default, so stdin is only an opt-in (`make` reads `Makefile`).
    default_program: bool,
}

/// Creates a stdin REPL that needs `operands` operands to name its program.
const fn repl(name: &'static str, operands: usize) -> Repl {
    Repl {
        name,
        operands,
        valued: &[],
        program: &[],
        interactive: &[],
        default_program: false,
    }
}

const MAKE_PROGRAM: &[&str] = &["-f", "--file", "--makefile"];
const LUA_VALUED: &[&str] = &["-l", "-j"];
const SQLITE_VALUED: &[&str] = &[
    "-cmd",
    "-separator",
    "-newline",
    "-nullvalue",
    "-vfs",
    "-mmap",
    "-heap",
    "-maxsize",
    "-escape",
    "-init",
];

const REPLS: &[Repl] = &[
    Repl {
        valued: &[
            "-I",
            "-r",
            "-E",
            "--encoding",
            "--prompt",
            "--prompt-mode",
            "--back-trace-limit",
            "--context-mode",
        ],
        ..repl("irb", 1)
    },
    Repl {
        valued: &["-e", "--exec", "-r", "--require", "-I", "-c", "--context"],
        ..repl("pry", 1)
    },
    Repl {
        valued: LUA_VALUED,
        program: &["-e"],
        interactive: &["-i"],
        ..repl("lua", 1)
    },
    Repl {
        valued: LUA_VALUED,
        program: &["-e"],
        interactive: &["-i"],
        ..repl("luajit", 1)
    },
    Repl {
        valued: SQLITE_VALUED,
        interactive: &["-interactive"],
        ..repl("sqlite", 2)
    },
    Repl {
        program: MAKE_PROGRAM,
        default_program: true,
        ..repl("make", 0)
    },
    Repl {
        program: MAKE_PROGRAM,
        default_program: true,
        ..repl("gmake", 0)
    },
    Repl {
        valued: &["-g", "--gui", "-d", "--debugger", "--encoding"],
        program: &["-f", "--file", "-e", "CMD"],
        interactive: &["--interactive"],
        ..repl("R", 0)
    },
    Repl {
        valued: &["-encoding"],
        ..repl("tclsh", 1)
    },
    Repl {
        valued: &["-L", "-C", "-x", "-l", "-e", "--language"],
        program: &["-s", "-c"],
        ..repl("guile", 1)
    },
    Repl {
        program: &["-e"],
        ..repl("ghci", 0)
    },
    Repl {
        valued: &[
            "--class-path",
            "-classpath",
            "-cp",
            "--module-path",
            "--add-modules",
            "--startup",
            "--feedback",
        ],
        ..repl("jshell", 0)
    },
];

/// Denies a REPL that reads its commands from stdin: no program, a `-` program, or a flag that
/// starts the REPL after the program.
fn repl_program(spec: &Repl, args: &[Arg]) -> Outcome {
    let mut program = spec.default_program;
    let (mut info, mut interactive, mut ended) = (false, false, false);
    let mut operands = 0;
    let mut i = 0;
    while let Some(a) = args.get(i) {
        let t = a.text.as_str();
        i += 1;
        if !ended && t == "--" {
            ended = true;
        } else if ended || t == "-" || !t.starts_with('-') {
            if t == "-" || reads_stdin(a) {
                return Outcome::Deny(STDIN_REPL);
            }
            operands += 1;
            if operands == spec.operands || (!ended && spec.program.contains(&t)) {
                program = true;
                break;
            }
        } else if let Some(given) = match_opt(t, spec.program) {
            let value = match given {
                OptValue::Inline(v) => Some(v),
                OptValue::Next => {
                    i += 1;
                    args.get(i - 1).map(|n| n.text.as_str())
                }
            };
            if value == Some("-") {
                return Outcome::Deny(STDIN_REPL);
            }
            program = true;
        } else if spec.interactive.contains(&t) {
            interactive = true;
        } else if INFO_FLAGS.contains(&t) {
            info = true;
        } else if match_opt(t, spec.valued) == Some(OptValue::Next) {
            i += 1;
        }
    }
    if !interactive && (program || info) {
        Outcome::Allow
    } else {
        Outcome::Deny(STDIN_REPL)
    }
}

/// Function calls in a make variable value that run code: a shell, a file read, or make source.
const MAKE_CALLS: &[&str] = &[
    "$(shell", "${shell", "$(file", "${file", "$(eval", "${eval", "$(guile", "${guile",
];

/// A make variable value calls a `MAKE_CALLS` function. make reads its environment as variables
/// too, so `CFLAGS='$(shell CMD)' make` runs CMD when a recipe expands `$(CFLAGS)`.
fn make_runs_code(text: &str) -> bool {
    MAKE_CALLS.iter().any(|call| text.contains(call))
}

/// make runs `--eval` text as makefile source. A command-line variable runs a shell with `!=`,
/// and a `MAKE_CALLS` function in its value runs code. The hook cannot see what either one does.
fn make_evals(args: &[Arg]) -> bool {
    let mut options = true;
    for a in args {
        let t = a.text.as_str();
        if options && t == "--" {
            options = false;
        } else if let Some(long) = t.strip_prefix("--").filter(|_| options) {
            let name = long.split('=').next().unwrap_or(long);
            if is_prefix_of("eval", name, 2) {
                return true;
            }
        } else if let Some(cluster) = t.strip_prefix('-').filter(|_| options) {
            // A short option cluster (`-kE TEXT`). An option with a value ends the cluster.
            for c in cluster.chars() {
                if c == 'E' {
                    return true;
                }
                if "CfIjlOoW".contains(c) {
                    break;
                }
            }
        } else if let Some((name, _)) = t.split_once('=') {
            if name.ends_with('!') || make_runs_code(t) {
                return true;
            }
        }
    }
    false
}

/// sqlite3 dot-commands that read or write files, load an extension, run a shell or a program,
/// run SQL from a table (`.selftest`), or change safety settings (`.dbconfig`). Each entry is a
/// name and the shortest abbreviation that sqlite3 3.54 accepts or the rules match.
const SQLITE_DOT_IO: &[(&str, usize)] = &[
    ("shell", 2),
    ("system", 2),
    ("read", 3),
    ("import", 2),
    ("output", 1),
    ("once", 2),
    ("load", 2),
    ("log", 2),
    ("save", 2),
    ("backup", 2),
    ("restore", 3),
    ("open", 2),
    ("excel", 3),
    ("www", 3),
    ("clone", 3),
    ("archive", 2),
    ("iotrace", 2),
    ("trace", 2),
    ("selftest", 4),
    ("session", 3),
    ("dbconfig", 3),
];

/// sqlite3 names that read or write files, load an extension, run a shell (`edit`), or run SQL
/// text built at run time: `sha1_query` and `sha3_query` run their argument, and
/// `writable_schema` and `sqlite_dbpage` can rewrite the schema.
const SQLITE_DENIED: &[&str] = &[
    "readfile",
    "writefile",
    "fsdir",
    "zipfile",
    "load_extension",
    "edit",
    "sqlar_compress",
    "sqlar_uncompress",
    "sha1_query",
    "sha3_query",
    "sqlite_dbpage",
    "writable_schema",
];

/// sqlite3 reads `--name` as `-name` before a `--` that ends its options.
fn sqlite_args(args: &[Arg]) -> Vec<Arg> {
    let mut ended = false;
    args.iter()
        .map(|a| {
            let mut a = a.clone();
            ended |= a.text == "--";
            if !ended && a.text.starts_with("--") {
                a.text.remove(0);
            }
            a
        })
        .collect()
}

/// sqlite3 runs each operand after the database, and each `-cmd` value, as SQL or a
/// dot-command. `-A` runs `.archive`, which reads and writes files, and `-init FILE` runs FILE,
/// so a parse error prints its text.
fn sqlite_io(args: &[Arg]) -> bool {
    let (mut operands, mut ended, mut i) = (0, false, 0);
    while let Some(a) = args.get(i) {
        let t = a.text.as_str();
        i += 1;
        if !ended && t == "--" {
            ended = true;
            continue;
        }
        if ended || t == "-" || !t.starts_with('-') {
            operands += 1;
            if operands > 1 && sql_io(t) {
                return true;
            }
            continue;
        }
        if t.starts_with("-A") || match_opt(t, &["-archive"]).is_some() {
            return true;
        }
        let Some((name, given)) = ["-cmd", "-init"]
            .into_iter()
            .find_map(|n| Some((n, match_opt(t, &[n])?)))
        else {
            if match_opt(t, SQLITE_VALUED) == Some(OptValue::Next) {
                i += 1;
            }
            continue;
        };
        let value = match given {
            OptValue::Inline(v) => v,
            OptValue::Next => {
                i += 1;
                args.get(i - 1).map_or("", |n| n.text.as_str())
            }
        };
        let io = if name == "-init" {
            value != "/dev/null"
        } else {
            sql_io(value)
        };
        if io {
            return true;
        }
    }
    false
}

/// SQL or a dot-command that `SQLITE_DOT_IO` or `SQLITE_DENIED` names. A dot-command quotes its
/// arguments like a shell, and `.parameter set` runs a value as SQL, so in text with a
/// dot-command any whole word counts. In SQL only identifiers count. A name in a string literal
/// does not run, because `SQLITE_DENIED` holds every way to run SQL text built at run time.
fn sql_io(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    let mut dot = false;
    for line in lower.lines() {
        let Some(command) = line.trim_start().strip_prefix('.') else {
            continue;
        };
        dot = true;
        let (word, _) = first_word(command);
        if SQLITE_DOT_IO
            .iter()
            .any(|&(full, min)| abbrev(word, full, min))
        {
            return true;
        }
    }
    if dot {
        return SQLITE_DENIED.iter().any(|name| sql_word(&lower, name));
    }
    sql_identifiers(&lower)
        .iter()
        .any(|word| SQLITE_DENIED.contains(word))
}

/// The text holds `name` as a whole word.
fn sql_word(text: &str, name: &str) -> bool {
    let ident = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '$';
    text.match_indices(name)
        .any(|(i, _)| !text[..i].ends_with(ident) && !text[i + name.len()..].starts_with(ident))
}

/// The identifiers in SQL text: bare words, and names in `"double quotes"`, `[brackets]`, or
/// backticks. String literals and comments are skipped, as the SQLite tokenizer skips them.
fn sql_identifiers(sql: &str) -> Vec<&str> {
    let ident = |c: u8| c.is_ascii_alphanumeric() || c == b'_' || c == b'$' || c >= 0x80;
    let b = sql.as_bytes();
    let mut words = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let close = match b[i] {
            b'[' => Some(b']'),
            q @ (b'\'' | b'"' | b'`') => Some(q),
            _ => None,
        };
        if let Some(close) = close {
            // A doubled quote is a literal quote; a bracket has no escape.
            let mut end = i + 1;
            while end < b.len() && b[end] != close {
                end += 1;
            }
            while close != b']' && b.get(end + 1) == Some(&close) {
                end += 2;
                while end < b.len() && b[end] != close {
                    end += 1;
                }
            }
            if b[i] != b'\'' {
                words.push(&sql[i + 1..end.min(b.len())]);
            }
            i = end + 1;
        } else if sql[i..].starts_with("--") {
            i = sql[i..].find('\n').map_or(b.len(), |n| i + n);
        } else if sql[i..].starts_with("/*") {
            i = sql[i + 2..].find("*/").map_or(b.len(), |n| i + n + 4);
        } else if ident(b[i]) {
            let start = i;
            while i < b.len() && ident(b[i]) {
                i += 1;
            }
            words.push(&sql[start..i]);
        } else {
            i += 1;
        }
    }
    words
}
