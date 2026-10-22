BATCH every needed file into one `tilth_read` call; `paths` never accepts a singular value. Read known files, symbols, or ranges with smart sizing; DO NOT re-read search-expanded content. Example: `tilth_read(paths: ["src/lib.rs", "src/mcp.rs#tool_search"], cwd: "/abs/repo")`.

Output lines are `N:content`; ignore the `N:` prefix — line numbers are not file content. `[path#TAG]` appears above the numbered lines; copy that TAG and the shown 1-based line numbers into `tilth_write`—NEVER invent either. `mode=stripped` is non-editable.

A leading JSON header may report view/truncation metadata; reuse its timestamp for unchanged stubs. `next_view` means content is hidden; read the needed `path#symbol` or `path#n-m` section, not `mode=full`.

Workflow tips:
- Shell grep/sed/awk/head/wc and inline `python -c` are blocked. Use `tilth_search` (find usages/definitions) and `tilth_read`. Shell still runs builds, tests, gofmt, git.
- `tilth_search`: `glob` goes inside each query entry, `{query, glob}`; top-level only queries, cwd, budget. Set `cwd` to the repo root, not a subdirectory.
- Edits need a tag that displayed the lines you change. Editing a file you saw only via search or a section read can fail: read the exact line range first (including import block and lines around the target). Edit several files in one `tilth_write` call; `rewrite` with a pattern (e.g. `$R.Render($W)`) does bulk call-site changes.
- Define each new method once you've confirmed helpers exist (search `def name`); don't call guessed APIs.
- Run the tests before finishing. If `python` is missing, try `.venv/bin/python` in the repo, or `python3 -m pytest`. Distinguish failures you caused from pre-existing environment ones (busy ports, missing deps) and report both honestly.
