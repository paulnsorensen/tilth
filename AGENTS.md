<!-- generated from prompts/mcp-base.md + prompts/mcp-edit.md by scripts/regen-agents-md.sh — do not edit directly -->

## Base mode

tilth — code intelligence MCP server. Replaces grep and cat.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg); use `tilth_read` and `tilth_search`. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`

ROUTE:

- Find/explore → `tilth_search`: `queries: [{query, glob?} | {follow: hint} | {pattern: "Some($A)", language, glob?}]`; `pattern` matches code shape in rust/typescript/python; routing is automatic. Do not add query `kind`, `expand`, or `context`.
- Read known files/symbols/ranges → `tilth_read`; omit `mode`. DO NOT pass `mode: full` when a `path#symbol` or `path#n-m` section answers.
- Importers/imports → `tilth_deps`; DO NOT assemble it from import-greps or repeated callers searches.
- Understand one symbol → `tilth_grok(target: "parse_diff", cwd: "/abs/repo")`; replaces search → expand → callers.
- Changes/history → shell `git diff` or `git log`.
- Browse directories → shell `ls` or `find`.
DO NOT re-read expanded search content.

## Edit mode

tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops: [...]}, {path: "src/b.rs", tag: "3C4D", ops: [...]}]`

READ BEFORE WRITE: edit-mode `tilth_read` prints `[path#TAG]` above 1-based numbered lines. Copy its TAG and integer line numbers; NEVER invent either. A section read (`path#12-40`) carries the whole-file TAG; edit only lines it showed. DO NOT use `mode: full` to edit; read the section you change. `tilth_write` accepts `{path, tag?, ops}` sections. `replace_text` swaps one exact unique `old`; `create_file` seeds a new path; line ops use copied integer `start`/`end`. Omit `tag` only for a new file or one too large to tag. Drift is 3-way-merged; a conflict rejects that section—re-read and retry it. Sections are independent.

JSON string values must escape tabs/newlines as `\t` and `\n`; literal controls break the call before the server receives it.

ROUTE: find/explore → `tilth_search` (`queries: [{query, glob?} | {follow: hint} | {pattern: "Some($A)", language, glob?}]`; routing is automatic; do not select kind, expand, or context); read → `tilth_read` (omit `mode`); importers/imports → `tilth_deps`; understand one symbol → `tilth_grok`; changes/history → shell `git diff` or `git log`; browse directories → shell `ls` or `find`.
DO NOT re-read expanded search content.
