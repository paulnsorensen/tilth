tilth_edit: Batch edit files using hash-anchored lines. Replaces the host Edit tool.

ALWAYS group edits to multiple files into ONE tilth_edit call (max 20 files). Never call tilth_edit twice in a row.

Workflow: tilth_read → copy anchors (<line>:<hash>) (BOTH line and hash required) → pass to tilth_edit.
Note: tilth_search does NOT provide hashes — you MUST tilth_read the file or section first to get them.

Request shape:
```json
{
  "files": [
    {
      "path": "a.rs",
      "edits": [
        {"start": "<line>:<hash>", "content": "<new code>"},
        {"start": "<line>:<hash>", "end": "<line>:<hash>", "content": "..."},
        {"start": "<line>:<hash>", "content": ""}
      ]
    },
    {"path": "b.rs", "edits": [...]}
  ],
  "diff": true
}
```

Edit forms inside `edits`:
- Single line: {"start": "<line>:<hash>", "content": "<new code>"}
- Range: {"start": "<line>:<hash>", "end": "<line>:<hash>", "content": "..."}
- Delete: {"start": "<line>:<hash>", "content": ""}

Behavior:
- Each file is processed independently. A hash mismatch on one file does NOT block the others.
- Hash mismatch means the file changed after you read it. Re-read THAT file and retry (other files in the batch already applied).
- Large files: tilth_read shows outline — use section to get hashlined content.
- Pass diff: true to see a compact before/after diff per file.
- After editing a function signature, tilth_edit shows callers that may need updating.

DO NOT use the host Edit tool. Use tilth_edit for all edits.
