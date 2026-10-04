tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops: [...]}, {path: "src/b.rs", tag: "3C4D", ops: [...]}]`

SEE BEFORE WRITE: `tilth_read` and `tilth_search` show `[path#TAG]` and numbered lines. Copy the TAG and shown line numbers; never invent them. A section read carries the file TAG. Edit only shown lines. DO NOT use `mode: full` to edit; read the section you change. `tilth_write` takes `{path, tag?, ops}` sections: `replace_text` swaps one exact `old` (`all: true` for every match, `count: N` for exactly N); `rewrite` swaps outermost, non-overlapping ast-grep matches (`count: N` counts those matches); `create_file` seeds a new path; line ops use copied integer `start`/`end`. Omit `tag` only for new or untaggable files. Drift merges or rejects each section; re-read after conflict.

JSON string values must escape tabs/newlines as `\t` and `\n`; literal controls break the call before the server receives it.

ROUTE: find → `tilth_search` (`queries: [{query, glob?} | {follow: hint} | {pattern: "Some($A)", language, glob?}]`); read → `tilth_read` (omit `mode`); edit → `tilth_write`; review → shell `git diff` or `git log`; browse directories → shell `ls` or `find`. Do not select search kind or context.
DO NOT re-read expanded search content.