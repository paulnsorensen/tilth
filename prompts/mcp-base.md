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