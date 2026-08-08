<!-- generated from prompts/mcp-base.md + prompts/mcp-edit.md by scripts/regen-agents-md.sh — do not edit directly -->

## Base mode

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

## Edit mode

tilth — code intelligence MCP server. Replaces grep, cat, find, ls, git diff, and host edit tools.

DO NOT call bare names; use full names such as `mcp__tilth__tilth_write` and `mcp__tilth__tilth_read`.

DO NOT omit `cwd`: set it to the absolute checkout directory on every call. Relative paths/scopes anchor there; absolute paths pass through. The server cannot see your shell cwd; `..` in relative paths is refused.

BATCH related work; array parameters never accept singular values:
- `queries: [{query: "foo"}, {query: "bar", kind: "symbol"}]`
- `paths: ["src/a.rs#12-40", "src/b.rs#parse"]`
- `patterns: ["*.rs", "*.toml"]`
- `edits: [{path: "src/a.rs", tag: "1A2B", ops}, {path: "src/b.rs", tag: "3C4D", ops}]`
Every call also needs `cwd: "/abs/repo"`.

READ BEFORE WRITE: edit-mode `tilth_read` prints `[path#TAG]` above 1-based numbered lines. Copy its TAG and integer line numbers; NEVER invent either. `tilth_write` accepts `{path, tag?, ops}` sections. Line ops use copied integer `start`/`end`, not find/replace. Omit `tag` only for a new file. Drift is 3-way-merged; a conflict rejects that section—re-read and retry it. Sections are independent.

JSON string values must escape tabs/newlines as `\t` and `\n`; literal controls break the call before the server receives it.

ROUTE: find/explore → `tilth_search`; read → `tilth_read`; importers/imports → `tilth_deps`; understand one symbol → `tilth_grok`; changes → `tilth_diff`; browse → `tilth_list`; edit → `tilth_write`.

DO NOT cat/head/tail/sed repo files via shell; use `tilth_read`.
DO NOT grep/rg/ls/find via shell; use `tilth_search`/`tilth_list`.
DO NOT use shell git diff/log or host Edit/Write; use tilth tools.
Shell is for tests, builds, and non-file operations.
DO NOT re-read expanded search content.
