<!-- generated from prompts/mcp-base.md + prompts/mcp-edit.md by scripts/regen-agents-md.sh — do not edit directly -->

tilth — AST-aware code intelligence MCP server.

Four tools, all batch-capable via array inputs:
  • tilth_search — find by symbol/content/regex/callers (merged by default)
  • tilth_read   — load files with smart auto-sizing (full / signature / preview)
  • tilth_list   — directory layout with token-cost rollups
  • tilth_write  — hash-anchored / overwrite / append; auto-fixes safe mismatches

Each tool's description carries detailed usage, examples, and batching rules.
DO NOT use host Grep, Read, Glob, Write, or Edit.

Aux tools: tilth_deps (blast-radius before signature changes), tilth_diff (structural diff: [+] added, [-] deleted, [~] body changed, [~:sig] signature changed)

tilth_write: Hash-anchored / overwrite / append edits. Replaces the host Edit and Write tools.

Per-file mode (default "hash"):
  • hash       — replace lines at hash-anchored positions from tilth_read or tilth_search expanded output
  • overwrite  — replace whole file contents; creates the file if absent
  • append     — append bytes to file; creates if absent

ALWAYS group edits to all ready files into ONE tilth_write call (max 20 files). Never call tilth_write twice in a row.

Request shape:
```json
{
  "files": [
    {"path": "a.rs", "mode": "hash", "edits": [
      {"start": "<line>:<hash>", "content": "<new code>"},
      {"start": "<line>:<hash>", "end": "<line>:<hash>", "content": "..."},
      {"start": "<line>:<hash>", "content": ""}
    ]},
    {"path": "new.rs", "mode": "overwrite", "content": "..."},
    {"path": "log.txt", "mode": "append",    "content": "...\n"}
  ],
  "diff": true
}
```

Hash-mode behavior:
- Each file is processed independently. A hash mismatch on one file does NOT block the others.
- Auto-fix on mismatch: tilth re-reads the file and fingerprints your original anchor range body. If the fingerprint appears at exactly one new location, the edit is applied there and the response notes `auto-fixed: <old_line> → <new_line>`. Zero or 2+ matches → fresh hashlined content of the affected region is returned inline so you can retry in one turn.
- A malformed edit entry fails that whole file.
- Each path may appear only once per call.

Pass `diff: true` when you need to confirm exactly what changed and don't already have the new content cached.

DO NOT use the host Edit or Write tool. Use tilth_write for all writes.

(Legacy alias: tilth_edit accepts the same hash-mode shape.)
