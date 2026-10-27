tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.
Shell BLOCKS cat/head/tail/sed/awk/wc/grep/rg/git grep on repo files, `>` redirects to files, heredocs, and inline interpreters (`python -c`, `node -e`). Instead: tilth_read/tilth_search for content; tilth_write to create a script or fixture, then run that file with Bash. Use a unique scratch dir (`mktemp -d`), never a fixed /tmp name.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths anchor there (`..` refused); absolute paths pass through. The server cannot see your shell cwd.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops: [...]}, {path: "src/b.rs", tag: "3C4D", ops: [...]}]`

DO NOT use `mode: full` to edit; read the section you change. Read structural ranges before `rewrite`.

A `tilth_write` result returns the new tag and changed line ranges; lines you wrote are editable from it with no read; read a range for any other line.

ROUTE: find/explore → `tilth_search`; read → `tilth_read` (omit `mode`).
DO NOT re-read expanded search content.