//! Fail-closed shell guard for coding-agent hosts.
//!
//! The guard reads one host pre-tool event, parses the shell command with `tree-sitter-bash`,
//! and denies shell file reads, searches, and writes. It only denies; it never rewrites.
//! This module imports only `std`, `serde_json`, `tree_sitter`, and `tree_sitter_bash`.

mod hints;
mod rules;
mod shape;
#[cfg(test)]
mod tests;
mod walk;

use std::io::{Read, Write};
use std::str::FromStr;

/// The host family that names the event shape on stdin.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Host {
    /// `tool_input.command` (Claude Code, Codex, Droid, Crush, Augment).
    #[default]
    Claude,
    /// `tool_input.command` (Copilot CLI).
    Copilot,
    /// Top-level `command` (Cursor `beforeShellExecution`).
    Cursor,
    /// `tool_info.command_line` (Windsurf `pre_run_command`).
    Windsurf,
}

impl FromStr for Host {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "claude" => Ok(Self::Claude),
            "copilot" => Ok(Self::Copilot),
            "cursor" => Ok(Self::Cursor),
            "windsurf" => Ok(Self::Windsurf),
            other => Err(format!(
                "unknown host `{other}`; expected claude, copilot, cursor, or windsurf"
            )),
        }
    }
}

/// Which tool names the deny reason cites.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Hint {
    /// The tilth MCP tools, with `cwd`.
    #[default]
    Tilth,
    /// The host's own Read, Grep, Glob, Edit, and Write tools.
    Native,
}

impl FromStr for Hint {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "tilth" => Ok(Self::Tilth),
            "native" => Ok(Self::Native),
            other => Err(format!("unknown hint `{other}`; expected tilth or native")),
        }
    }
}

/// The guard's decision for one command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict {
    Allow,
    Deny {
        /// The rule entry that fired.
        rule: &'static str,
        /// The text for stderr.
        reason: String,
    },
}

/// Decide one shell command. A parse with an `ERROR` node denies.
#[must_use]
pub fn verdict(command: &str, hint: Hint) -> Verdict {
    match walk::check(command) {
        None => Verdict::Allow,
        Some(rule) => Verdict::Deny {
            rule,
            reason: hints::reason(rule, hint),
        },
    }
}

fn event_verdict(host: Host, hint: Hint, input: &str) -> Verdict {
    let event: serde_json::Value = match serde_json::from_str(input) {
        Ok(v) => v,
        Err(_) => return deny(hints::MALFORMED_EVENT, hint),
    };
    match shape::read(host, &event) {
        shape::Shape::Allow => Verdict::Allow,
        shape::Shape::Deny(rule) => deny(rule, hint),
        shape::Shape::Command(command) => verdict(&command, hint),
    }
}

fn deny(rule: &'static str, hint: Hint) -> Verdict {
    Verdict::Deny {
        rule,
        reason: hints::reason(rule, hint),
    }
}

/// Evaluate one host event. Returns the process exit code: 0 allows, 2 denies.
/// A deny writes its reason to `err`. Nothing goes to stdout.
pub fn run_io(host: Host, hint: Hint, input: &str, err: &mut impl Write) -> i32 {
    match event_verdict(host, hint, input) {
        Verdict::Allow => 0,
        Verdict::Deny { reason, .. } => {
            let _ = writeln!(err, "{reason}");
            2
        }
    }
}

/// Entry for the CLI: parse the host and hint names, read stdin, and evaluate.
/// An unknown name writes the reason to stderr and returns 2.
#[must_use]
pub fn run_cli(host: &str, hint: &str) -> i32 {
    let mut err = std::io::stderr();
    let (host, hint) = match (host.parse::<Host>(), hint.parse::<Hint>()) {
        (Ok(host), Ok(hint)) => (host, hint),
        (Err(msg), _) | (_, Err(msg)) => {
            let _ = writeln!(err, "{msg}");
            return 2;
        }
    };
    let mut input = String::new();
    if std::io::stdin().read_to_string(&mut input).is_err() {
        let _ = writeln!(err, "{}", hints::reason(hints::MALFORMED_EVENT, hint));
        return 2;
    }
    run_io(host, hint, &input, &mut err)
}
