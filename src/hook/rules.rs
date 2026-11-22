//! The deny-list: one simple command in, an outcome out.

use super::hints::{
    Rule, AWK_INPLACE, DEBUGGER_SCRIPT, DEVICE_PATH, GIT_APPLY, GIT_CAT_FILE, GIT_CONFIG, GIT_GREP,
    GIT_SHOW, INPUT_REDIRECT, JSON_TOOL, OUTPUT_REDIRECT, PERL_I, PYTHON_M, SED_I, SHELL_STDIN,
    STDIN_REPL, UNRESOLVED,
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
    let (words, xargs) = match unwrap(cmd.words) {
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
            let wrapped = &cmd.words[..cmd.words.len() - words.len()];
            let env = cmd
                .assigns
                .iter()
                .chain(wrapped.iter().filter(|a| is_assign_word(&a.text)));
            if env.into_iter().any(|a| git_env_runs_command(&a.text)) {
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
            None => stdin_reader(name, args),
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
/// feeds it operands. `Err` holds the outcome when a runner runs shell source or Python itself.
fn unwrap(mut words: &[Arg]) -> Result<(&[Arg], bool), Outcome> {
    let mut xargs = false;
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
                Some(Run::No) | None => break,
            },
        };
        words = rest.get(i..).unwrap_or(&[]);
    }
    Ok((words, xargs))
}

/// How an option word gives its value.
#[derive(Clone, Copy, PartialEq, Eq)]
enum OptValue<'t> {
    /// The value is the next word.
    Next,
    /// The word holds the value (`-cVALUE`, `--name=VALUE`).
    Inline(&'t str),
}

/// Matches an option word against option names; `None` when no name matches. A short name
/// (`-c`) takes an attached value (`-cVALUE`). A long name takes `--name=VALUE`. A single-dash
/// long name (`-ex`, in gdb and sqlite3 style) also matches its double-dash spelling (`--ex`).
fn match_opt<'t>(t: &'t str, names: &[&str]) -> Option<OptValue<'t>> {
    let value = |after: &'t str| match after {
        "" => OptValue::Next,
        v => OptValue::Inline(v),
    };
    names.iter().filter(|n| n.starts_with('-')).find_map(|&n| {
        if n.len() == 2 {
            return t.strip_prefix(n).map(value);
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
/// list counts as a flag, so an unknown option never denies an ordinary command. The cost: the
/// value of an unlisted option reads as the command, and the rules check that word instead.
struct Runner {
    name: &'static str,
    /// Subcommands that run a command. Empty when the runner itself runs it.
    subs: &'static [&'static str],
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
    /// The runner runs Python: `-m MODULE` runs a module, and a `-` command reads stdin.
    python: bool,
}

/// Creates a runner that takes its command as the first operand and lists no options.
const fn runner(name: &'static str, subs: &'static [&'static str]) -> Runner {
    Runner {
        name,
        subs,
        at: CommandAt::Operand,
        positionals: 0,
        valued: &[],
        shell: &[],
        rest: &[],
        shell_mode: &[],
        python: false,
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
];
const NPM_VALUED: &[&str] = &["-p", "--package", "-w", "--workspace", "--prefix"];
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
const BUN_VALUED: &[&str] = &["-p", "--package"];
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
        ..runner("npx", &[])
    },
    Runner {
        valued: NPM_VALUED,
        shell: NPM_SHELL,
        ..runner("npm", &["exec", "x"])
    },
    Runner {
        valued: &["-C", "--directory", "-P", "--project"],
        ..runner("poetry", &["run"])
    },
    Runner {
        valued: &["--gemfile"],
        ..runner("bundle", &["exec"])
    },
    Runner {
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
        ..runner("bun", &["x"])
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
        valued: &["-e", "--environment", "--manifest-path", "--color"],
        ..runner("pixi", &["run"])
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
];

enum Run {
    /// Not a runner invocation (`mise install`, `bun run`).
    No,
    /// The command starts at this index of the runner's arguments.
    At(usize),
    /// The runner runs shell source or Python itself, with this outcome.
    Done(Outcome),
}

/// Finds the command that a runner invocation runs.
fn run_target(spec: &Runner, args: &[Arg]) -> Run {
    let is_opt = |t: &str| t.len() > 1 && t.starts_with('-') && t != "--";
    let skip = |t: &str| 1 + usize::from(match_opt(t, spec.valued) == Some(OptValue::Next));
    let mut i = 0;
    if !spec.subs.is_empty() {
        loop {
            let Some(t) = args.get(i).map(|a| a.text.as_str()) else {
                return Run::No;
            };
            if is_opt(t) {
                i += skip(t);
            } else if spec.subs.contains(&t) {
                i += 1;
                break;
            } else {
                return Run::No;
            }
        }
    }
    let mut positionals = spec.positionals;
    let mut shell_mode = false;
    while let Some(a) = args.get(i) {
        let t = a.text.as_str();
        if t == "--" || spec.rest.contains(&t) {
            return command_at(spec, args, i + 1, shell_mode);
        }
        if let Some(value) = match_opt(t, spec.shell) {
            return Run::Done(match value {
                OptValue::Inline(v) => reparse(&[Arg {
                    text: v.to_string(),
                    literal: a.literal,
                    opaque: a.opaque,
                }]),
                OptValue::Next => args
                    .get(i + 1)
                    .map_or(Outcome::Allow, |n| reparse(std::slice::from_ref(n))),
            });
        }
        if let Some(value) = match_opt(t, &["-m", "--module"]).filter(|_| spec.python) {
            let (module, after) = match value {
                OptValue::Inline(v) => (Some(v), i + 1),
                OptValue::Next => (args.get(i + 1).map(|n| n.text.as_str()), i + 2),
            };
            let leftover = args.get(after..).unwrap_or(&[]);
            return Run::Done(module.map_or(Outcome::Allow, |m| python_module(m, leftover)));
        }
        if is_opt(t) {
            shell_mode |= spec.shell_mode.contains(&t);
            i += skip(t);
        } else if spec.at == CommandAt::Option {
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
            return command_at(spec, args, i, shell_mode);
        }
    }
    match spec.at {
        CommandAt::Option => Run::Done(Outcome::Deny(SHELL_STDIN)),
        _ => Run::At(args.len()),
    }
}

/// The runner's command starts at `k`. Shell mode runs the words as shell source, and a Python
/// runner reads a `-` script from stdin.
fn command_at(spec: &Runner, args: &[Arg], k: usize, shell_mode: bool) -> Run {
    let words = args.get(k..).unwrap_or(&[]);
    if shell_mode && !words.is_empty() {
        return Run::Done(reparse(words));
    }
    if spec.python && words.first().is_some_and(|w| w.text == "-") {
        return Run::Done(Outcome::Deny("python"));
    }
    Run::At(k.min(args.len()))
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

/// Routes a command that is not an inline interpreter to the debugger or stdin REPL rules.
fn stdin_reader(name: &str, args: &[Arg]) -> Outcome {
    let stem = name.trim_end_matches(|c: char| c.is_ascii_digit() || matches!(c, '.' | '-'));
    match stem {
        "gdb" | "rust-gdb" | "gdb-multiarch" => debugger(true, args),
        "lldb" | "rust-lldb" => debugger(false, args),
        _ => REPLS
            .iter()
            .find(|r| r.name == stem)
            .map_or(Outcome::Allow, |spec| repl_program(spec, args)),
    }
}

/// gdb and lldb read commands from stdin unless `-batch` stops them. A command option can run a
/// shell command, which the rules reparse, or a pipe or script interpreter, which denies.
fn debugger(gdb: bool, args: &[Arg]) -> Outcome {
    let (commands, files, batch): (&[&str], &[&str], &[&str]) = if gdb {
        (
            &["-ex", "-eval-command", "-iex", "-init-eval-command"],
            &["-x", "-command", "-ix", "-init-command"],
            &["-batch", "-batch-silent"],
        )
    } else {
        (
            &[
                "-o",
                "--one-line",
                "-O",
                "--one-line-before-file",
                "-k",
                "--one-line-on-crash",
            ],
            &[
                "-s",
                "--source",
                "-S",
                "--source-before-file",
                "-K",
                "--source-on-crash",
            ],
            &["-b", "-batch"],
        )
    };
    let mut scripts: Vec<String> = Vec::new();
    let (mut batched, mut info) = (false, false);
    let mut i = 0;
    while let Some(a) = args.get(i) {
        let t = a.text.as_str();
        i += 1;
        if t == "--" || (gdb && matches!(t, "--args" | "-args")) {
            break;
        }
        let file = match_opt(t, files);
        if let Some(given) = match_opt(t, commands).or(file) {
            let (value, opaque) = if let OptValue::Inline(v) = given {
                (v, a.opaque)
            } else {
                let Some(next) = args.get(i) else { break };
                i += 1;
                (next.text.as_str(), next.opaque)
            };
            if file.is_some() {
                if value == "-" {
                    return Outcome::Deny(STDIN_REPL);
                }
                continue;
            }
            if opaque {
                return Outcome::Deny(UNRESOLVED);
            }
            match debugger_command(value) {
                Ok(Some(source)) => scripts.push(source),
                Ok(None) => {}
                Err(rule) => return Outcome::Deny(rule),
            }
        } else if match_opt(t, batch) == Some(OptValue::Next) {
            batched = true;
        } else if INFO_FLAGS.contains(&t) {
            info = true;
        }
    }
    if !batched && !info {
        Outcome::Deny(STDIN_REPL)
    } else if scripts.is_empty() {
        Outcome::Allow
    } else {
        Outcome::Reparse(scripts.join("\n"))
    }
}

/// Classifies one gdb or lldb command. `Ok(Some(source))` is a shell command to reparse.
fn debugger_command(text: &str) -> Result<Option<String>, Rule> {
    let text = text.trim_start();
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
    let interpreter = matches!(head, "py" | "pi" | "gu")
        || ["pipe", "python", "python-interactive", "guile", "script"]
            .iter()
            .any(|full| abbrev(head, full, 3));
    if interpreter {
        Err(DEBUGGER_SCRIPT)
    } else {
        Ok(None)
    }
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
        valued: &[
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
        ],
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
