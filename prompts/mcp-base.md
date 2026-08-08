tilth — code intelligence MCP server. Replaces grep, cat, find, ls, and git diff.

DO NOT call bare names; use full names such as `mcp__tilth__tilth_search` and `mcp__tilth__tilth_read`.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:
- `queries: [{query: "foo"}, {query: "bar", kind: "symbol"}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `patterns: ["*.rs", "*.toml"]`
Every call also needs `cwd: "/abs/repo"`.

ROUTE:
- Find/explore → `tilth_search`; omitted `kind` merges definitions, usages, and callers.
- Read known files/symbols/ranges → `tilth_read`.
- Importers/imports → `tilth_deps`; DO NOT assemble the blast radius manually.
- Understand one symbol → `tilth_grok(target: "parse_unified_diff", cwd: "/abs/repo")`.
- Changes → `tilth_diff(cwd: "/abs/repo")` or `tilth_diff(source: "HEAD~1", cwd: "/abs/repo")`.
- Browse without a search term → `tilth_list`.

DO NOT cat/head/tail/sed repo files via shell; use `tilth_read`.
DO NOT grep/rg/ls/find via shell; use `tilth_search`/`tilth_list`.
DO NOT use shell git diff/log; use `tilth_diff`.
Shell is for tests, builds, and non-file operations.
DO NOT re-read expanded search content.