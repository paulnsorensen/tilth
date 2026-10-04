tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops: [...]}, {path: "src/b.rs", tag: "3C4D", ops: [...]}]`

SEE BEFORE WRITE: Only source text carries `[path#TAG]` and line numbers. Read structural ranges before `rewrite`. Copy shown TAGs and line numbers; never invent them. Section reads carry file TAGs. Edit only shown lines. DO NOT use `mode: full` to edit; read the section you change. `tilth_write` takes `{path, tag?, ops}`: `replace_text` swaps exact `old` (`all: true` replaces all; `count: N` requires N); `rewrite` swaps outermost non-overlapping ast-grep matches (`count: N` requires N); `create_file` seeds a new path; line ops use copied integer `start`/`end`. Omit `tag` only for new/untaggable files. Drift merges or rejects per section; re-read conflicts.

Pair `tilth_write` with the next `tilth_read`.

JSON string values must escape tabs/newlines as `\t` and `\n`; literal controls break the call before the server receives it.

ROUTE: find/explore → `tilth_search` (`queries: [{query, glob?} | {follow: hint} | {pattern: "Some($A)", language, glob?}]`; routing is automatic); repeated call-site edits → `pattern` search, then `rewrite`; read → `tilth_read` (omit `mode`); changes/history → shell `git diff` or `git log`; browse directories → shell `ls` or `find`.
DO NOT re-read expanded search content.