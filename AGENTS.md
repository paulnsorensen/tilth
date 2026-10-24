<!-- generated from prompts/mcp.md by scripts/regen-agents-md.sh — do not edit directly -->

tilth — code intelligence MCP server. Replaces grep, cat, and host edit tools.
DO NOT use shell for repo content reads (cat/head/tail/sed/grep/rg) and DO NOT use host Edit/Write; use tilth tools. Shell is for directory browsing (ls/find), Git review/history, tests, builds, and non-file operations.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths anchor there (`..` refused); absolute paths pass through. The server cannot see your shell cwd.

BATCH related work; array parameters never accept singular values:

- `queries: [{query: "foo"}, {query: "bar", glob: "*.rs"}]` or `[{follow: hint}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops: [...]}, {path: "src/b.rs", tag: "3C4D", ops: [...]}]`

DO NOT use `mode: full` to edit; read the section you change. Read structural ranges before `rewrite`.

Reuse a `tilth_write` result's tag and lines; skip re-reads.

ROUTE: find/explore → `tilth_search`; read → `tilth_read` (omit `mode`).
DO NOT re-read expanded search content.
