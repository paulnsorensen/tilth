tilth — code intelligence MCP server. Use it instead of grep/cat/find/ls.

## Fast path

1. Search first: use tilth_search for symbols, text, usages, and callers. It often returns enough source to avoid a read.
2. Batch every independent operation. One tool call is one turn.
3. Do not re-read source already expanded by search results.
4. Use host Bash only for builds/tests or commands tilth cannot do.

## Batching patterns

- Many files, same smart behavior: `tilth_read({ "paths": ["a.rs", "b.rs"] })`.
- Many sections in one file: `tilth_read({ "path": "a.rs", "sections": ["10-40", "90-130"] })`.
- Mixed files/sections/full reads: `tilth_read({ "files": [{ "path": "a.rs", "sections": ["10-40"] }, { "path": "b.rs", "full": true }] })`.
- Many globs: `tilth_files({ "patterns": ["src/**/*.rs", "tests/**/*.rs"] })`.
- Many symbols: `tilth_search({ "query": "parse_anchor,apply_batch,tool_read" })` (max 5).

Prefer these batched shapes before making a second tilth call.

## Tools

tilth_search: AST-aware code search.
- `query`: symbol, literal text, regex, or comma-separated symbols (max 5).
- `kind`: `symbol` declarations (default), `any`, `content`, `regex`, `callers`.
- `expand`: number of top matches to inline.
- `context`: path being edited, for ranking nearby results.
- `glob`: include/exclude files, e.g. `"*.rs"`, `"!*.test.ts"`, `"src/**/*.{ts,tsx}"`.

tilth_read: Smart file reading.
- `path`: one file. `section`: one range/heading. `sections`: many ranges/headings from that file.
- `paths`: up to 20 files with the same smart behavior.
- `files`: up to 20 per-file specs, each with `path`, optional `section`, `sections`, or `full`.
- Large files outline first; read only the needed sections afterward.

tilth_files: File discovery.
- `pattern`: one glob. `patterns`: up to 20 globs in one call.
- Use instead of directory listing commands.

tilth_deps: Blast-radius check only before renaming/removing exports, changing signatures, or changing behavior callers rely on.

tilth_diff: Structural diff.
- No args: uncommitted overview.
- `scope`: file/directory, `search`: filter, `expand`: changed symbols to show, `blast`: caller warnings.
- `source`: `uncommitted`, `staged`, a ref/range, or use `a`+`b`, `patch`, or `log`.
- Output markers: [+] added, [-] deleted, [~] body changed, [~:sig] signature changed
