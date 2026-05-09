<!-- generated from prompts/mcp-base.md + prompts/mcp-edit.md by scripts/regen-agents-md.sh — do not edit directly -->

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

tilth_edit: Batch editing with hashline anchors from tilth_read.

## Edit workflow

1. Read once, in batches, before editing:
   - Different files/sections: `tilth_read({ "files": [{ "path": "a.rs", "sections": ["10-40", "90-120"] }, { "path": "b.rs", "section": "5-30" }] })`.
   - One file with disjoint edit sites: use `sections`, not serial reads.
2. Copy exact `line:hash` anchors from hashline output.
3. Send all independent edits in one tilth_edit call:
   - One file: one `files` entry with many `edits`.
   - Many files: many `files` entries in the same call.
4. If a file reports a hash mismatch, re-read only that file/section and retry only its failed edits.

## Request shape

```json
{
  "files": [
    {
      "path": "a.rs",
      "edits": [
        { "start": "10:abc", "content": "replacement line" },
        { "start": "20:def", "end": "25:123", "content": "multi\nline\nreplacement" },
        { "start": "40:999", "content": "" }
      ]
    }
  ],
  "diff": true
}
```

- `start`/`end`: anchors from tilth_read hashlines. Omit `end` for a single-line replace.
- `content`: replacement text. Empty string deletes the line/range.
- `diff: true`: include compact before/after output.
- Each file applies independently; one file's failure does not block the others.
- After signature changes, tilth_edit may append caller blast-radius warnings.

DO NOT use the host Edit tool. Use tilth_edit for all edits.
