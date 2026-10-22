//! Host event shapes: where each family keeps the shell command.

use super::hints::{Rule, MISSING_COMMAND};
use super::Host;
use serde_json::Value;

/// Tool names that run a shell command on some host.
const SHELL_TOOLS: &[&str] = &[
    "Bash",
    "bash",
    "Execute",
    "launch-process",
    "run_shell_command",
    "shell",
    "run_command",
];

pub(super) enum Shape {
    Command(String),
    Allow,
    Deny(Rule),
}

/// Extracts the host command, allowing known non-shell events and denying invalid shapes.
pub(super) fn read(host: Host, event: &Value) -> Shape {
    if !event.is_object() {
        return Shape::Deny(super::hints::MALFORMED_EVENT);
    }
    let command = match host {
        Host::Claude | Host::Copilot => event.pointer("/tool_input/command"),
        Host::Cursor => event.pointer("/command"),
        Host::Windsurf => event.pointer("/tool_info/command_line"),
    };
    if let Some(Value::String(command)) = command {
        return Shape::Command(command.clone());
    }
    match host {
        Host::Claude | Host::Copilot => match event.get("tool_name").and_then(Value::as_str) {
            Some(name) if !SHELL_TOOLS.contains(&name) => Shape::Allow,
            _ => Shape::Deny(MISSING_COMMAND),
        },
        Host::Cursor | Host::Windsurf => Shape::Deny(MISSING_COMMAND),
    }
}
