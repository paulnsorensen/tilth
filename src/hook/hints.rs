//! Rule names, rule kinds, and deny-reason text.

use super::Hint;

/// A rule name. Every deny cites exactly one.
pub(super) type Rule = &'static str;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Read,
    Search,
    Write,
    Generic,
}

pub(super) const PARSE_ERROR: Rule = "parse error";
pub(super) const UNRESOLVED: Rule = "shell -c unresolved";
pub(super) const SHELL_STDIN: Rule = "shell stdin";
pub(super) const DEVICE_PATH: Rule = "device path";
pub(super) const INPUT_REDIRECT: Rule = "input redirect";
pub(super) const OUTPUT_REDIRECT: Rule = "output redirect";
pub(super) const GIT_SHOW: Rule = "git show rev:path";
pub(super) const GIT_CAT_FILE: Rule = "git cat-file";
pub(super) const GIT_GREP: Rule = "git grep";
pub(super) const GIT_APPLY: Rule = "git apply";
pub(super) const JSON_TOOL: Rule = "python -m json.tool";
pub(super) const SED_I: Rule = "sed -i";
pub(super) const PERL_I: Rule = "perl -i";
pub(super) const AWK_INPLACE: Rule = "awk -i inplace";

/// Host-event rules. They never come from a command string, so the corpus does not cover them.
pub(super) const MALFORMED_EVENT: Rule = "malformed event";
pub(super) const MISSING_COMMAND: Rule = "missing command";

const READ_RULES: &[Rule] = &[
    "cat",
    "tac",
    "head",
    "tail",
    "less",
    "more",
    "nl",
    "bat",
    "sed",
    "awk",
    "gawk",
    "mawk",
    "gsed",
    "sort",
    "uniq",
    "cut",
    "wc",
    "tr",
    "diff",
    "cmp",
    "od",
    "xxd",
    "hexdump",
    "strings",
    "base64",
    "jq",
    "yq",
    INPUT_REDIRECT,
    "find -exec",
    "find -execdir",
    "find -ok",
    "find -okdir",
    GIT_SHOW,
    GIT_CAT_FILE,
    JSON_TOOL,
    DEVICE_PATH,
];

const SEARCH_RULES: &[Rule] = &["grep", "egrep", "fgrep", "rg", "ag", "ack", GIT_GREP];

const WRITE_RULES: &[Rule] = &[
    OUTPUT_REDIRECT,
    "tee",
    SED_I,
    PERL_I,
    AWK_INPLACE,
    "patch",
    GIT_APPLY,
    "dd",
    "truncate",
    "python",
    "perl",
    "ruby",
    "node",
    "deno",
    "bun",
    "php",
];

#[cfg(test)]
const OTHER_RULES: &[Rule] = &[PARSE_ERROR, UNRESOLVED, SHELL_STDIN];

/// Every rule name the command rules can cite.
#[cfg(test)]
pub(super) fn all_rules() -> Vec<Rule> {
    [READ_RULES, SEARCH_RULES, WRITE_RULES, OTHER_RULES].concat()
}

/// Classifies a rule as a read, search, write, or generic denial.
fn kind(rule: &str) -> Kind {
    if READ_RULES.contains(&rule) {
        Kind::Read
    } else if SEARCH_RULES.contains(&rule) {
        Kind::Search
    } else if WRITE_RULES.contains(&rule) {
        Kind::Write
    } else {
        Kind::Generic
    }
}

/// Formats a denial reason with the tool names selected by `hint`.
pub(super) fn reason(rule: &str, hint: Hint) -> String {
    let kind = kind(rule);
    match hint {
        Hint::Tilth => tilth_reason(rule, kind),
        Hint::Native => native_reason(rule, kind),
    }
}

/// Formats a denial reason with tilth tool calls for the rule's kind.
fn tilth_reason(rule: &str, kind: Kind) -> String {
    let read = "mcp__tilth__tilth_read(paths:[\"<path>\"], cwd:\"<checkout>\")";
    let search = "mcp__tilth__tilth_search(queries:[{query:\"<pattern>\"}], cwd:\"<checkout>\")";
    let write = "mcp__tilth__tilth_write(edits:[{path:\"<path>\", tag:\"<TAG>\", ops:[{op:\"replace_text\", old:\"...\", new:\"...\"}]}], cwd:\"<checkout>\")";
    match kind {
        Kind::Read => format!(
            "Blocked: shell `{rule}` reads file content. Use tilth_read, not a shell reader.\n\n\
             Run instead:\n  {read}\n\n\
             Append #start-end or #symbol to a path to read one section. \
             Filters on command output (`git log | head`) still run."
        ),
        Kind::Search => format!(
            "Blocked: shell `{rule}` searches files. Use tilth_search, not a raw search tool.\n\n\
             Run instead:\n  {search}\n\n\
             Filters on command output (`git log | grep fix`) still run."
        ),
        Kind::Write => format!(
            "Blocked: shell `{rule}` writes files or runs inline code. Use tilth_write.\n\n\
             Read the file with tilth_read first to get its [path#TAG], then:\n  {write}\n\n\
             For a new file, omit the tag and use op create_file."
        ),
        Kind::Generic => format!(
            "Blocked: the hook cannot allow this command (`{rule}`). \
             Read files with tilth_read, search with tilth_search, and write with tilth_write."
        ),
    }
}

/// Formats a denial reason with the host's native tool names.
fn native_reason(rule: &str, kind: Kind) -> String {
    match kind {
        Kind::Read => format!(
            "Blocked: shell `{rule}` reads file content. Use the Read tool, not a shell reader. \
             Use the Glob tool to list files."
        ),
        Kind::Search => format!(
            "Blocked: shell `{rule}` searches files. Use the Grep tool, not a raw search tool. \
             Use the Glob tool to find file names."
        ),
        Kind::Write => format!(
            "Blocked: shell `{rule}` writes files or runs inline code. \
             Use the Edit tool to change an existing file and the Write tool to create a new one."
        ),
        Kind::Generic => format!(
            "Blocked: the hook cannot allow this command (`{rule}`). \
             Use the Read, Grep, Glob, Edit, and Write tools for file work."
        ),
    }
}
