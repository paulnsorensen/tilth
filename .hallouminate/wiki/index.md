# tilth wiki — index

This wiki is what an LLM working in the `tilth` repo writes to and reads from
when it wants to remember things across sessions. It lives at
`.hallouminate/wiki/` and is indexed as the `repo:tilth:wiki` corpus, separate
from the source-code corpus (`repo:tilth:corpus`) and the per-session reports
under `.cheese/`.

## Topics

- [Edit-anchor design: per-line hash vs whole-file tag](edit-anchor-design.md) — why tilth originally anchored edits with a per-line content hash, the FNV low-bit-mask bug, the measured ~25% per-read token tax vs oh-my-pi's O(1) whole-file tag, and the analysis behind the since-shipped switch to the whole-file-tag model.
- [MCP cwd / workspace-root binding](mcp-cwd-root-binding.md) — why tilth uses a required per-call `cwd` param (renamed from `root` in PR #113; not the MCP `roots` capability) to resolve paths to the right git-worktree checkout; the silent worktree gotcha; 8-harness client survey.


- [`tilth_read` budget accounting and vacuous budget guards](read-budget-accounting.md) — `finalize_response` is the only budget gate and `record_savings` has two easily-conflated call sites; why a `<= budget` assertion is vacuous on Linux CI (50-token flat header reserve) while failing on macOS, why the obvious token-count differential is also wrong (`estimate_tokens` is subadditive), and the exact-equality assertion that works. Also the known macOS-only `batch_budget_represents_every_query` failure.

## How to use this index

`index.md` is a table of contents, not a topic. Add new pages to the list
above (alphabetical), keeping a one-line gloss per entry. Anything substantive
belongs in a topic file — one topic per file.

If you read this index and don't see the topic you need, run `list_files`
against the `repo:tilth:wiki` corpus first — the index may be out of date
relative to the directory.
