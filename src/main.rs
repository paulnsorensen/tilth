use std::io::{self, IsTerminal, Write};
use std::path::PathBuf;
use std::process;

use clap::{CommandFactory, Parser};
use clap_complete::Shell;

/// tilth — Tree-sitter indexed lookups, smart code reading for AI agents.
/// One tool replaces `read_file`, grep, glob, `ast_grep`, and find.
#[derive(Parser)]
#[command(name = "tilth", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,

    /// File path, symbol name, glob pattern, or text to search.
    query: Option<String>,

    /// Directory to search within or resolve relative paths against.
    #[arg(long, default_value = ".")]
    scope: PathBuf,

    /// Line range or markdown heading (e.g. "45-89" or "## Architecture"). Bypasses smart view.
    #[arg(long)]
    section: Option<String>,

    /// Max tokens in response. Reduces detail to fit.
    #[arg(long)]
    budget: Option<u64>,

    /// Force full output (effect depends on query type — see --help).
    ///
    /// File path: return the whole file instead of an outline (bypass smart view).
    ///
    /// Symbol / text / regex: inline source for every match (equivalent to
    /// `--expand=<all>`). Explicit `--expand=N` wins. Output stays bounded
    /// by `--budget`.
    ///
    /// Glob: no effect (glob queries already return a flat file list).
    #[arg(long)]
    full: bool,

    /// Machine-readable JSON output.
    #[arg(long)]
    json: bool,

    /// Run as MCP server (JSON-RPC on stdio).
    #[arg(long)]
    mcp: bool,

    /// Accepted for backwards compatibility and ignored: the MCP server always
    /// serves the edit surface.
    #[arg(long = "edit", hide = true)]
    _edit: bool,

    /// Inline source for top N search matches (default 2 when flag bare).
    ///
    /// Applies to symbol / text / regex queries. Without the flag the
    /// result is just the outline summary. `--full` upgrades this to
    /// expand every match (subject to `--budget`); explicit `--expand=N`
    /// wins over `--full`. No effect on file-path or glob queries.
    #[arg(long, num_args = 0..=1, default_missing_value = "2", require_equals = true)]
    expand: Option<usize>,

    /// File pattern filter (e.g. "*.rs", "!*.test.ts", "*.{go,rs}").
    #[arg(long)]
    glob: Option<String>,

    /// Find all callers of a symbol.
    #[arg(long)]
    callers: bool,

    /// Print shell completions for the given shell.
    #[arg(long, value_name = "SHELL")]
    completions: Option<Shell>,
}

#[derive(clap::Subcommand)]
enum Command {
    /// Install tilth into an MCP host's config.
    /// Supported hosts: claude-code, cursor, windsurf, vscode, claude-desktop, opencode, gemini, codex, amp, droid, antigravity, zed, copilot-cli, augment, kiro, kilo-code, cline, roo-code, trae, qwen-code, crush, pi
    Install {
        /// MCP host to configure.
        host: String,

        /// Accepted for backwards compatibility and ignored: the MCP server
        /// always serves the edit surface.
        #[arg(long = "edit", hide = true)]
        _edit: bool,

        /// Also register the shell guard (`tilth hook`) in the host's hook config.
        /// Supported hosts: claude-code, codex, droid, crush, augment, cursor, windsurf.
        #[arg(long)]
        hook: bool,
    },
    /// Shell guard for agent hosts: read one pre-tool event on stdin, exit 2 to deny.
    Hook {
        /// Event shape: claude, copilot, cursor, or windsurf. Also accepts the install host
        /// names claude-code, codex, droid, crush, and augment (family claude).
        #[arg(long, value_enum, default_value = "claude")]
        host: HookHost,

        /// Tool names the deny reason cites: tilth or native.
        #[arg(long, default_value = "tilth")]
        hint: String,
    },
    /// Start Claude Code with the tilth preamble as its system prompt.
    /// `claude` must be the first argument. Every later argument passes to `claude` unchanged.
    /// A system-prompt flag from the caller replaces the preamble.
    #[command(disable_help_flag = true, override_usage = "tilth claude [ARGS]...")]
    Claude,
    /// Start Codex with the tilth preamble as its model instructions.
    /// `codex` must be the first argument. Every later argument passes to `codex` unchanged.
    /// A `model_instructions_file` override from the caller replaces the preamble.
    #[command(disable_help_flag = true, override_usage = "tilth codex [ARGS]...")]
    Codex,
    /// Print the tilth preamble, so that you can give it to another harness.
    /// With `--path`, write it to the cache file and print the path of that file.
    Preamble {
        /// Print the path of the cache file, not the text.
        #[arg(long)]
        path: bool,
    },
}

/// Accepted `tilth hook --host` values: the four event families, plus the install host
/// names that share a family.
#[derive(Clone, Copy, clap::ValueEnum)]
enum HookHost {
    #[value(aliases = ["claude-code", "codex", "droid", "crush", "augment"])]
    Claude,
    Copilot,
    Cursor,
    Windsurf,
}

impl HookHost {
    fn family(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Copilot => "copilot",
            Self::Cursor => "cursor",
            Self::Windsurf => "windsurf",
        }
    }
}

/// `tilth claude ...` and `tilth codex ...` dispatch before clap parses argv.
/// Clap would drop a leading `--`, and every argument must reach the harness unchanged.
fn launch_from_argv() -> Option<i32> {
    let mut argv = std::env::args_os().skip(1);
    let harness = match argv.next()?.to_str()? {
        "claude" => tilth::launch::Harness::Claude,
        "codex" => tilth::launch::Harness::Codex,
        _ => return None,
    };
    Some(tilth::launch::run(harness, argv.collect()))
}

fn main() {
    if let Some(code) = launch_from_argv() {
        process::exit(code);
    }
    configure_thread_pools();
    let cli = Cli::parse();

    // Shell completions
    if let Some(shell) = cli.completions {
        clap_complete::generate(shell, &mut Cli::command(), "tilth", &mut io::stdout());
        return;
    }

    // Subcommands
    if let Some(cmd) = cli.command {
        match cmd {
            Command::Install { ref host, hook, .. } => {
                let result = if hook {
                    tilth::install::run_with_hook(host)
                } else {
                    tilth::install::run(host)
                };
                if let Err(e) = result {
                    eprintln!("install error: {e}");
                    process::exit(1);
                }
            }
            Command::Hook { host, ref hint } => {
                process::exit(tilth::hook::run_cli(host.family(), hint));
            }
            Command::Preamble { path } => {
                process::exit(tilth::launch::print_preamble(path));
            }
            // `launch_from_argv` handles `claude` / `codex` as argv[1], so clap reaches
            // these only when another argument comes first.
            Command::Claude | Command::Codex => {
                Cli::command()
                    .error(
                        clap::error::ErrorKind::ArgumentConflict,
                        "`claude` or `codex` must be the first argument to tilth. Put harness arguments after it.",
                    )
                    .exit();
            }
        }
        return;
    }

    // MCP mode: JSON-RPC server
    if cli.mcp {
        // Pass --scope to MCP if it's not the default "."
        let mcp_scope = if cli.scope.as_os_str() == "." {
            None
        } else {
            Some(
                cli.scope
                    .canonicalize()
                    .unwrap_or_else(|_| cli.scope.clone()),
            )
        };
        if let Err(e) = tilth::mcp::run(mcp_scope.as_deref()) {
            eprintln!("mcp error: {e}");
            process::exit(1);
        }
        return;
    }

    let is_tty = io::stdout().is_terminal();

    // CLI mode: single query
    let Some(query) = cli.query else {
        eprintln!("usage: tilth <query> [--scope DIR] [--section N-M] [--budget N]");
        process::exit(3);
    };

    let cache = tilth::cache::OutlineCache::new();
    let scope = cli.scope.canonicalize().unwrap_or(cli.scope);

    // When piped (not a TTY), force full output — scripts expect raw content.
    // This promotion exists for FilePath queries (return full file instead of
    // outline) and is harmless for Glob (which ignores `full`). Search queries
    // also receive `full=true` here but stay outline-only — they do not auto-
    // expand on piping. See the `cli.full` guard on the expand override below.
    let full = cli.full || !is_tty;

    // Explicit `--full` on a search query means expand every match. Guarded on
    // `cli.full` (NOT the piped-derived `full` above) so that subprocess /
    // pipeline callers (Claude Code's Bash tool, CI scripts, `tilth foo | rg`)
    // still receive the concise outline they want. They opt into expand-all by
    // adding `--full` themselves. Explicit `--expand=N` still wins because it
    // produces `expand != 0`. We over-apply to all query types — `run_inner`
    // only forwards `expand` to search dispatches, so the value is silently
    // ignored for FilePath and Glob.
    //
    let expand = compute_expand(cli.expand, cli.full);

    // Callers mode
    if cli.callers {
        let result = tilth::run_callers(
            &query,
            &scope,
            expand,
            cli.budget,
            cli.glob.as_deref(),
            cli.full,
        );
        emit_result(result, &query, cli.json, is_tty);
        return;
    }

    let result = if expand > 0 {
        tilth::run_expanded(
            &query,
            &scope,
            cli.section.as_deref(),
            cli.budget,
            full,
            expand,
            cli.glob.as_deref(),
            &cache,
            cli.full,
        )
    } else if full {
        tilth::run_full(
            &query,
            &scope,
            cli.section.as_deref(),
            cli.budget,
            cli.glob.as_deref(),
            &cache,
        )
    } else {
        tilth::run(
            &query,
            &scope,
            cli.section.as_deref(),
            cli.budget,
            cli.glob.as_deref(),
            &cache,
        )
    };

    emit_result(result, &query, cli.json, is_tty);
}

fn emit_result(
    result: Result<String, tilth::error::TilthError>,
    query: &str,
    json: bool,
    is_tty: bool,
) {
    match result {
        Ok(output) => {
            if json {
                let json = serde_json::json!({
                    "query": query,
                    "output": output,
                });
                println!(
                    "{}",
                    serde_json::to_string_pretty(&json)
                        .expect("serde_json::Value is always serializable")
                );
            } else {
                emit_output(&output, is_tty);
            }
        }
        Err(e) => {
            eprintln!("{e}");
            process::exit(e.exit_code());
        }
    }
}

/// Write output to stdout. When TTY and output is long, pipe through $PAGER.
fn emit_output(output: &str, is_tty: bool) {
    let line_count = output.lines().count();
    let term_height = terminal_height();

    if is_tty && line_count > term_height {
        let pager = std::env::var("PAGER").unwrap_or_else(|_| "less".into());
        if let Ok(mut child) = process::Command::new(&pager)
            .arg("-R")
            .stdin(process::Stdio::piped())
            .spawn()
        {
            if let Some(ref mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(output.as_bytes());
            }
            let _ = child.wait();
            return;
        }
    }

    // Conventional CLI output ends with a newline so the next shell prompt
    // starts on its own line. Most internal formatters terminate with `\n`,
    // but the search-result footer (e.g. `(~507 tokens)`) and a few other
    // paths do not — guard at the sink rather than auditing every formatter.
    print!("{output}");
    if !output.ends_with('\n') {
        println!();
    }
    let _ = io::stdout().flush();
}

fn terminal_height() -> usize {
    // ioctl(TIOCGWINSZ) on stdout — the real terminal height. Bash maintains
    // $LINES as an unexported shell variable, so most subprocesses (us included)
    // never receive it; relying on it alone made tilth assume 24 rows in any
    // typical interactive shell and page nearly every result. Fall back to
    // $LINES then to 24 only if the ioctl is unavailable (tests, exotic TTYs).
    if let Some((_, terminal_size::Height(h))) = terminal_size::terminal_size() {
        return h as usize;
    }
    if let Ok(lines) = std::env::var("LINES") {
        if let Ok(h) = lines.parse::<usize>() {
            return h;
        }
    }
    24
}

/// Configure rayon global thread pool to limit CPU usage.
///
/// Defaults to min(cores / 2, 6). Override with `TILTH_THREADS` env var.
/// This matters for long-lived MCP sessions where back-to-back searches
/// can sustain high CPU (see #27).
fn configure_thread_pools() {
    let num_threads = std::env::var("TILTH_THREADS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or_else(|| {
            std::thread::available_parallelism().map_or(4, |n| (n.get() / 2).clamp(2, 6))
        });

    rayon::ThreadPoolBuilder::new()
        .num_threads(num_threads)
        .build_global()
        .ok();
}

/// Compute the effective `expand` value for a search query from the raw
/// CLI flags. Lifted out of `main` so the `--full` / `--expand` precedence
/// is unit-testable.
///
/// Precedence:
/// - Explicit `--expand=N` always wins (`cli_expand = Some(n)`), even alongside `--full`.
/// - Bare `--full` with no `--expand` → `FULL_EXPAND_CAP` (50).
/// - Neither flag → 0 (no expansion).
///
/// Critically, `cli_full` is the *parsed* `--full` flag, NOT the piped-derived
/// `full = cli.full || !is_tty` in `main`. Subprocess / pipeline callers
/// (Claude Code's Bash tool, CI scripts, `tilth foo | rg`) must keep the
/// concise outline by default; expand-all is opt-in via explicit `--full`.
fn compute_expand(cli_expand: Option<usize>, cli_full: bool) -> usize {
    /// `--budget` already bounds output, but `expand=usize::MAX` makes tilth
    /// compute the expanded source for every match before truncating —
    /// wasted parsing + rendering on pathological queries. 50 is well above
    /// any practical "show me everything that matters" case (`MAX_MATCHES` is
    /// 10 for symbol search anyway).
    const FULL_EXPAND_CAP: usize = 50;
    match (cli_expand, cli_full) {
        (Some(n), _) => n,
        (None, true) => FULL_EXPAND_CAP,
        (None, false) => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Pin `--expand=N` precedence — explicit value always wins, including
    /// when combined with `--full`.
    #[test]
    fn explicit_expand_wins_over_full() {
        assert_eq!(compute_expand(Some(2), false), 2);
        assert_eq!(compute_expand(Some(2), true), 2);
        assert_eq!(compute_expand(Some(0), false), 0);
        assert_eq!(compute_expand(Some(0), true), 0);
        assert_eq!(compute_expand(Some(99), true), 99);
    }

    /// Pin `--full` → expand=50 when no explicit `--expand`.
    #[test]
    fn bare_full_promotes_to_full_expand_cap() {
        assert_eq!(compute_expand(None, true), 50);
    }

    /// Pin the default — neither flag means no expansion.
    #[test]
    fn neither_flag_means_zero_expand() {
        assert_eq!(compute_expand(None, false), 0);
    }

    /// `install <host> --edit` is a retired no-op flag; old scripts that
    /// still pass it must keep parsing.
    #[test]
    fn install_accepts_retired_edit_flag() {
        let cli = Cli::try_parse_from(["tilth", "install", "claude-code", "--edit"])
            .expect("install --edit must still parse");
        assert!(matches!(
            cli.command,
            Some(Command::Install { ref host, .. }) if host == "claude-code"
        ));
    }

    #[test]
    fn install_hook_flag_parses() {
        let cli = Cli::try_parse_from(["tilth", "install", "claude-code", "--hook"]).unwrap();
        assert!(matches!(
            cli.command,
            Some(Command::Install { hook: true, .. })
        ));
        let cli = Cli::try_parse_from(["tilth", "install", "claude-code"]).unwrap();
        assert!(matches!(
            cli.command,
            Some(Command::Install { hook: false, .. })
        ));
    }

    #[test]
    fn hook_subcommand_defaults_and_flags() {
        let cli = Cli::try_parse_from(["tilth", "hook"]).unwrap();
        assert!(matches!(
            cli.command,
            Some(Command::Hook { host, ref hint }) if host.family() == "claude" && hint == "tilth"
        ));
        let cli =
            Cli::try_parse_from(["tilth", "hook", "--host", "cursor", "--hint", "native"]).unwrap();
        assert!(matches!(
            cli.command,
            Some(Command::Hook { host, ref hint }) if host.family() == "cursor" && hint == "native"
        ));
        for alias in ["claude-code", "codex", "droid", "crush", "augment"] {
            let cli = Cli::try_parse_from(["tilth", "hook", "--host", alias]).unwrap();
            assert!(matches!(
                cli.command,
                Some(Command::Hook { host, .. }) if host.family() == "claude"
            ));
        }
        assert!(Cli::try_parse_from(["tilth", "hook", "--host", "bogus"]).is_err());
    }

    /// Pin the regression that 16212fc was authored to prevent: a piped
    /// invocation (where `main` sets `full = !is_tty = true` for `FilePath`
    /// queries) must still receive `expand=0` here. `compute_expand` only
    /// sees the parsed `cli.full`, never the piped-derived bool — so a
    /// future refactor that conflates the two would have to change this
    /// function's signature, making the violation visible.
    #[test]
    fn piped_invocation_does_not_auto_expand() {
        // Simulating: user ran `tilth foo` (no --full) but stdout is piped.
        // `main` will set `full = !is_tty = true` for downstream FilePath
        // handling, but cli.full stays false. compute_expand must return 0.
        let cli_full = false; // user did NOT pass --full
        assert_eq!(compute_expand(None, cli_full), 0);
    }
}
