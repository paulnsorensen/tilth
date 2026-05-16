# MCP — 27-day production feedback fixes

**Source:** `/Users/paul/Dev/tilth/.cheese/research/tilth-mcp-27d-analysis.md`
(cross-ref: `woz-mcp-12h-analysis.md`)

**Stack base:** `paulnsorensen/diff-23-24` (PR 25, head `5f367853`). PR 25 is itself
stacked on PR 24, so this branch becomes PR 26 in the stack.

**Out of scope (already addressed or held):**

- **F4 — `kind` reframe.** PR 25 commit `5f36785` already added the "Defaults
  are tuned — omit `kind` unless you specifically need to filter" lead sentence
  with a merged-default example. No further work.
- **F8 — `tilth_deps` / `tilth_diff`.** Keep both unchanged. 8 calls / 4 sessions
  and 2 calls / 2 sessions respectively over 27d; sample is too small to act on
  and the killer-feature nudge is deferred. Note in spec to revisit if a future
  analytics run shows continued decay.

---

## Goal

Six surgical fixes against production usage data from 117 sessions / 2,042 MCP
calls. Each fix has a measurable verifier: re-querying
`~/.claude/analytics/sessions.duckdb` after a bench run should move the cited
metric off zero (or, for batching, lower call-count per correct answer).

## Findings & fixes

### F1 — Structured cache token

**Problem.** `if_modified_since` round-trip = 0 / 2,042. `Results as of <ts>` is
emitted as a prose header line via `src/mcp/iso.rs::with_header` and documented
in prose in `prompts/tools/*.md`. Agents reliably extract structured fields,
unreliably round-trip prose values.

**Fix.** Replace the prose header with a leading text block whose entire body is
a JSON object the agent can pattern-match on.

Current response shape (all tools):

```
Results as of 2026-05-14T07:23:17Z

<payload body>
```

New response shape:

```json
{"if_modified_since": "2026-05-14T07:23:17Z"}
```

followed by `<payload body>` as a separate text block (or as the second segment
of the same string — implementer's call, but the JSON line must stand alone so a
trivial JSON-line parse pulls the field). The JSON object stays on one line.

**Touched files**

- `src/mcp/iso.rs::with_header` — switch leading line from `Results as of …` to
  `{"if_modified_since": "<ts>"}`.
- `src/mcp/iso.rs::unchanged_stub` — depends on `results_header()` today. Either
  drop the prose prefix internally or keep a private `iso_ts(now)` helper that
  both call.
- `prompts/tools/search.md`, `read.md`, `list.md` — update the
  `if_modified_since` mention to point at the new JSON-line shape.
- `prompts/mcp-base.md` — single mention of the cache token (if any) updated.
- Tests in `src/mcp/iso.rs` — adjust `with_header_prefixes_results_as_of` and
  any caller-side assertions on the prose form.

**Verify**

- After one bench run, `SELECT … json_extract_string(input, '$.if_modified_since')`
  on `mcp_calls` returns `uses_imss > 0`.
- All existing `Results as of` assertions in tests pass after rewriting to the
  new shape.

### F2 — Per-tool batching directives

**Problem.** Back-to-back same-session call rates: search 83%, read 72%, edit
60%. Array-form adoption: search 0.2%, read 19%, list 3%, edit 9%. Only
`prompts/tools/write.md` carries the imperative `ALWAYS group … ONE tilth_write
call. Never call tilth_write twice in a row.` Per-tool descriptions outweigh
base server-instructions at decision time.

**Fix.** Prepend a one-line imperative to each of the three weakly-batched
tools, mirroring `write.md` verbatim in shape.

Specifically (after applying, the lead sentences become):

- `prompts/tools/search.md` line 1 — prepend
  `ALWAYS group every search you need into ONE tilth_search call via queries:
  [...]. Never call tilth_search twice in a row.`
- `prompts/tools/read.md` line 1 — prepend
  `ALWAYS group every file you need into ONE tilth_read call via paths: [...].
  Never call tilth_read twice in a row.`
- `prompts/tools/list.md` line 1 — prepend
  `ALWAYS group every pattern you need into ONE tilth_list call via patterns:
  [...]. Never call tilth_list twice in a row.`

Then regen `AGENTS.md` via `scripts/regen-agents-md.sh`.

**Touched files**

- `prompts/tools/search.md`
- `prompts/tools/read.md`
- `prompts/tools/list.md`
- `AGENTS.md` (regenerated artifact)

**Verify** (haiku bench, per CLAUDE.md's "Benchmarks" section):

```bash
python benchmark/run.py --models haiku --reps 3 \
  --tasks rg_search_dispatch,rg_trait_implementors,gin_servehttp_flow \
  --modes tilth
```

Expect mean `tool_calls.tilth_search` per task to drop vs the same bench on
`paulnsorensen/diff-23-24` baseline. Cost-per-correct should not regress
materially.

### F3 — Empty-result differentiation

**Problem.** 38% of tilth_search calls (191 / 498) return zero matches. Today's
emission is a single conflated header line: `# Search: "<q>" in <scope> — 0
matches`. Agents can't distinguish "no glob match" from "files matched but no
content matches" from "files matched but no symbol matches" — driving 3× woz's
probe-and-adjust loop rate.

**Fix.** When `total == 0`, emit three counts plus a hint conditioned on which
case fired.

New emission shape (replaces the single-line header when `total == 0`):

```
# Search: "<q>" in <scope> — 0 matches
  Files matched glob: 47
  Files searched:     47
  Content hits:       0
  Hint: regex matched zero content; try kind: symbol or a broader pattern.
```

Hint dispatch table (apply first that matches):

| Condition | Hint |
|---|---|
| `files_matched_glob == 0` | `glob matched no files — broaden glob or check path` |
| `kind == "symbol"` and zero symbol hits | `no symbols matched; try kind: content or check spelling` |
| `kind == "content"` or `"regex"` and zero content hits | `regex matched zero content; try kind: symbol or a broader pattern` |
| `kind == "callers"` and zero call sites | `no callers found — re-check the symbol name; consider kind: symbol to verify it exists` |
| default (merged kind, all zero) | `no matches in any mode — re-check the query and glob` |

**Touched files**

- `src/format.rs::search_header` — extend signature to accept the three counts
  (or add a sibling `search_empty_header` helper). Caller in
  `src/search/mod.rs` populates the counts from existing walker state.
- `src/search/mod.rs` — wherever `search_header` is invoked with `total == 0`,
  pass the new counts and pick the hint.
- Add a unit test in `src/format.rs` (or `src/search/mod.rs`) for each of the
  five hint branches.

**Verify**

- New unit tests assert the literal hint text per branch.
- Spot-check: run `tilth_search` from a manual session with a deliberately
  wrong regex and confirm the response shows the three counts + a hint.

### F5 — Drop dead `context` field

**Problem.** `context` on `tilth_search` was used 0 / 498 times. The current
prompt suggests `If editing src/edit.rs, pass context: "src/edit.rs"` — agents
don't reliably track their own current file.

**Fix.** Remove `context` from the schema and the prompt entirely.

**Touched files**

- `src/mcp/tools/search.rs` — drop the `context` parameter from the tool
  input struct (and any handler that reads it).
- `src/mcp/tools/definitions.rs` — drop the `context` property from the JSON
  schema for `tilth_search`.
- `prompts/tools/search.md` — drop the `If editing src/edit.rs, pass context: …`
  sentence.
- `src/search/mod.rs` — if any code path uses `context` for ranking, drop the
  branch. Confirm during implementation; do not preserve as dead code.
- Tests — remove any `context`-related assertions.

**Verify**

- `cargo test` passes.
- A `tilth_search` call with `context` set returns an "unknown field" error
  (or is silently ignored — implementer's call, but JSON-schema-strict is
  preferred).

### F6 — Anchor counter-example in write.md

**Problem.** 10+ of 27 production edit errors are agents pasting bad anchors:
including the body (`20:7ae|    def create_run(`), bare line numbers (`20`),
trailing pipes (`20:7ae|`). PR 25 added output-side anchor docs in read.md and
search.md ("copy everything before `|`"), but write.md's input-side description
has no counter-example.

**Fix.** Add a WRONG/RIGHT block to `prompts/tools/write.md`, after the request
shape and before the modes paragraph.

Block content:

```
Anchor grammar — only the `<line>:<hash>` prefix, no body, no pipe, no bare line:

WRONG: "20:7ae|    def create_run("    do NOT include the body
WRONG: "20"                             hash is required
WRONG: "20:7ae|"                        drop the trailing pipe
RIGHT: "20:7ae"
```

Then regen `AGENTS.md`.

**Touched files**

- `prompts/tools/write.md`
- `AGENTS.md` (regenerated artifact)

**Verify**

- After bench run + a few days of production usage, re-query
  `tilth_edit` error breakdown:
  ```sql
  SELECT substr(tr.content, 1, 200) AS snippet, COUNT(*) AS n
  FROM mcp_calls mc JOIN tool_results tr ON mc.tool_use_id = tr.tool_use_id
  WHERE mc.tool_name LIKE '%tilth_edit' AND tr.is_error = 'true'
  GROUP BY snippet ORDER BY n DESC LIMIT 10;
  ```
  Expect `invalid start anchor '<line>:<hash>|…'` and `invalid start anchor '<bare>'`
  rates to drop.

### F7 — Audit the hash-mismatch auto-fix path

**Problem.** 0 / 257 production `tilth_edit` responses contained `auto-fixed:`,
yet `prompts/tools/write.md` advertises: "Hash mode auto-fixes safe mismatches:
if your anchor body appears at exactly one new location, the edit lands there
and the response notes `auto-fixed: <old_line> → <new_line>`."

The path exists at `src/mcp/tools/write.rs::probe_one_auto_fix` (not
`src/edit.rs` — PR 23 moved it). It has unit tests for the locate primitive in
`src/mcp/write.rs::auto_fix_locate` (4 tests). The unit tests pass. The path is
hooked into `append_per_file_auto_fix`, gated on `output.contains("hash
mismatch")`.

**Fix.** Two-step:

1. **Add an integration test** in `src/mcp/tools/write.rs` (or a new
   `tests/mcp_auto_fix.rs`) that simulates a realistic agent retry:
   - Write a file with content C0, capture a hashlined view at line 10.
   - Modify the file (insert 5 blank lines above the anchor target) →
     content C1.
   - Submit a `tilth_write` call with the old anchor from C0 against C1.
   - Assert the response contains the literal string `auto-fixed: 10 → 15`
     (or whatever the resolved new line is).

2. **If the test passes**, the path works in isolation — the prod-zero is a
   data-distribution issue (agents send anchors whose body doesn't survive
   intact; e.g., they typo the body or only the hash drifts). Tighten
   `prompts/tools/write.md` wording to advertise only what realistically fires:
   change "Hash mode auto-fixes safe mismatches" to a narrower claim like "Hash
   mode auto-fixes pure-whitespace shifts: if the anchor body survives byte-
   exact at one new location, the edit lands there."

   **If the test fails** (the literal `auto-fixed:` string never appears at the
   integration boundary), there's a wiring bug between
   `append_per_file_auto_fix` and the response formatter. Fix the wiring, then
   confirm the test passes.

**Touched files**

- New integration test (location TBD by implementer — likely
  `src/mcp/tools/write.rs` under `#[cfg(test)]`).
- Conditional: `prompts/tools/write.md` wording update.
- Conditional: `src/mcp/tools/write.rs` wiring fix.

**Verify**

- New integration test passes.
- If wording was tightened, re-read `prompts/tools/write.md` and confirm the
  advertised claim matches what `probe_one_auto_fix` actually emits.

---

## Cross-cutting acceptance criteria

1. `cargo test` passes.
2. `cargo clippy -- -D warnings` passes.
3. `cargo fmt --check` passes.
4. `scripts/regen-agents-md.sh` has been re-run; the diff against current
   `AGENTS.md` matches the prompt edits and nothing else.
5. Haiku bench on the three hard tasks runs and does not regress
   accuracy or cost-per-correct vs the PR 25 baseline.
6. Spot-check tilth's own session log: a fresh `tilth_search` call against this
   repo returns a response whose first line parses as
   `{"if_modified_since": "<ts>"}` and a follow-up call that echoes that field
   returns the same response with `unchanged @` stubs where appropriate.

## Stack and PR shape

- Branch: new branch off `paulnsorensen/diff-23-24` (PR 25 head `5f367853`).
- Suggested name: `paulnsorensen/mcp-27d-feedback`.
- Use `gt create` (or equivalent) so the stack tracks PR 25 as parent.
- One PR per Graphite convention; the spec is small enough that splitting per
  finding would be more overhead than signal.

## Open questions

None blocking. Two notes for the implementer:

- **F1's exact MCP content-block layout** — choose between (a) two `text`
  blocks where block 1 is `{"if_modified_since": "<ts>"}` and block 2 is the
  payload, or (b) one block whose first line is the JSON and remainder is the
  payload. (b) is simpler if the response builder is string-concatenation-
  shaped today; (a) is cleaner if it already builds typed blocks. Pick the
  smaller diff.
- **F7's test surface** — if `apply_batch` is awkward to call directly from a
  test, the test may need a fixture file plus a one-off harness. Acceptable;
  the cost is bounded.
