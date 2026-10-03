tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops: [...]}, {path: "src/b.rs", tag: "3C4D", ops: [...]}]`

SEE BEFORE WRITE: `tilth_read` prints `[path#TAG]` above 1-based numbered lines; `tilth_search` shows `path#TAG` too. Copy TAG and integer line numbers; NEVER invent either. Edit only lines a read or search showed. DO NOT use `mode: full` to edit; read the section you change. `tilth_write` accepts `{path, tag?, ops}` sections. `replace_text` swaps one exact `old` (`all: true` for every match); `rewrite` swaps every ast-grep `pattern` match; `create_file` seeds a new path; line ops use copied integer `start`/`end`. Omit `tag` only for a new file or one too large to tag. Conflicts reject that section—re-read and retry it.

JSON string values must escape tabs/newlines as `\t` and `\n`; literal controls break the call before the server receives it.

ROUTE: find/explore → `tilth_search` (`queries: [{query, glob?} | {follow: hint} | {pattern: "Some($A)", language, glob?}]`; routing is automatic); repeated call-site edits → `pattern` search, then `rewrite`; read → `tilth_read` (omit `mode`); changes/history → shell `git diff` or `git log`; browse directories → shell `ls` or `find`.
DO NOT re-read expanded search content.