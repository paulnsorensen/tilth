tilth_edit: Batch editing with hashline anchors from tilth_read.

## Edit workflow

1. Read once, in batches, before editing:
   - Different files/sections: `tilth_read({ "files": [{ "path": "a.rs", "sections": ["10-40", "90-120"] }, { "path": "b.rs", "section": "5-30" }] })`.
   - One file with disjoint edit sites: use `sections`, not serial reads.
2. Copy exact `line:hash` anchors from hashline output.
   - Search output has no edit hashes. Read target lines before editing.
   - Never invent anchors; re-read when anchors are missing or stale.
3. Send all independent edits in one tilth_edit call:
   - One file: one `files` entry with many `edits`.
   - Many files: many `files` entries in the same call.
4. If files report hash mismatches, batch re-read only those files/sections and retry only their failed edits.

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
