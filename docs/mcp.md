# MCP tools

This page describes the edit model and structural search of the fork's MCP server.
The exact text that the server sends is in [prompts/](../prompts/). That text wins over this page.

Every tool requires `cwd`, the absolute path of your checkout. A relative path anchors under `cwd`, and `..` is refused. An absolute path is used as given.

## Edit model

`tilth_read` prints a `[path#TAG]` header above numbered lines. `tilth_search` shows `path#TAG` in match headers.

```
[src/launch.rs#23C7]

9:const PREAMBLE: &str = include_str!("../prompts/preamble.md");
10:
11:/// A cache file of another preamble revision that no launch used for this long is removed.
12:const STALE_AFTER: Duration = Duration::from_hours(30 * 24);
```

`tilth_write` takes an `edits` array of `{path, tag, ops}` sections:

```json
{"path": "src/auth.ts", "tag": "1A2B", "ops": [
  {"op": "replace_text", "old": "compute()", "new": "recompute()", "all": true},
  {"op": "rewrite", "pattern": "$R.Render($W)", "rewrite": "$R.Render(ctx, $W)"}
]}
```

- Copy the TAG from the read or search. Do not make one up.
- Each op must touch only lines that a read or search showed.
- Omit `tag` only for a new file or a file that cannot have a tag.
- When the file changed after your read, tilth merges your ops onto the current file. The section fails only on a merge conflict.
- The result gives the new `[path#TAG]` and the changed line ranges. Lines you wrote stay editable with the new tag.

| Op | Fields |
|---|---|
| `replace_text` | `old`, `new`. Must match once. `all: true` replaces every match. `count: N` requires exactly N matches. |
| `rewrite` | `pattern`, `rewrite`, optional `count`. Replaces outermost, non-overlapping ast-grep matches. |
| `replace`, `delete` | `start`, `end` |
| `insert_before`, `insert_after` | `line` |
| `prepend`, `append` | `content` |
| `replace_block`, `delete_block`, `insert_after_block` | `at`: a line or `#symbol`. The block is the tree-sitter definition there. |
| `create_file` | `content`. Only for a path that does not exist. |
| `delete_file`, `move_file` | `move_file` takes `dest`. |

Large files come back as an outline. Read a `path#n-m` section to get numbered lines for the part you edit.

## Search entries

`tilth_search` takes `queries`, an array of 1 to 10 entries. Each entry has exactly one of these keys:

- `query`: a symbol, text, or regex. A query with `( ) [ ] * + ? | \ $ ^ { }` is a regex.
- `follow`: `{kind, target}`, where `kind` is `fetch_callers`, `fetch_callees`, `fetch_siblings`, or `fetch_tests`. Pass the `target` of an earlier result unchanged.
- `pattern`: an ast-grep pattern. It requires `language`.

Any entry can have a `glob`.

## Structural search

```json
{
  "cwd": "/absolute/project",
  "queries": [
    {"pattern": "Duration::from_hours($A)", "language": "rust", "glob": "src/*.rs"}
  ]
}
```

Result from this repository:

```json
{"query": "Duration::from_hours($A)", "resolved_as": "structural", "status": "ok",
 "completeness": "complete", "language": "rust", "view": "matches",
 "total_matches": 1, "files_matched": 1,
 "items": [{"path": "src/launch.rs",
            "matches": [[12, 12, 479, 508, {"A": [[12, 12, 500, 507]]}]]}]}
```

- `language` is one of `rust`, `typescript`, `tsx`, `javascript`, `python`, `go`, `java`, `scala`, `c`, `c++`, `ruby`, `php`, `swift`, `kotlin`, `c#`, `elixir`, or `bash`.
- `typescript` matches `.ts` files only. Use `tsx` for `.tsx` files.
- Each match is `[start_line, end_line, start_byte, end_byte]`. A fifth element maps each capture name to its ranges.
- Results hold ranges, not source text. Read the ranges before you use a `rewrite` op.
- An invalid pattern or an unknown language fails. It does not fall back to a text search.
- The scan keeps at most 1,000 matches. `total_matches` counts all matches.

### When the result is larger than the budget

The result takes the first view that fits, sets `completeness: "partial"` and `budget_limited: true`, and adds a `note`.

1. One matched file: the first matches of that file that fit. `shown` gives their count.
2. `view: "files"`: `[path, count]` pairs. This view is used only for 100 or fewer files.
3. `view: "directories"`: per-directory counts, plus `top_files` with up to 10 files.
4. `view: "none"`: totals only.

To narrow the result, run the entry again with `glob` set to a listed path or directory.
A root-level file name has no `/`, so prefix it with `/` to match only the root file.

## Session dedup

In one MCP session, a definition that a search already expanded shows as `[shown earlier]` with its location, not its body.
