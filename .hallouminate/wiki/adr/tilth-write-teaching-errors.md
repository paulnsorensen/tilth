# ADRs — tilth_write teaching errors (slug: tilth-write-teaching-errors)

Session 2026-08-02; spec at the durable corpus
(`paulnsorensen-tilth/specs/tilth-write-teaching-errors.md`). Evidence:
[[../usage-analytics-2026-07]] (July 2026 cross-harness analytics).

### ADR-001: Teaching errors only, no serde aliases [status: accepted]

- **Context:** ~74% of recent tilth_write errors are invented field/op names
  from Claude-family models (`find`, `anchor`, `text`, `seed`, …). Aliasing
  the pure renames (`text`→`content`, `new`→`content`) would prevent ~7 of
  ~48 recent errors outright.
- **Decision:** No aliases. Every wrong name errors, but the error carries
  the corrected example. Keeps tilth-write-json-ops ADR-003's
  one-advertised-grammar rule; schema stays byte-identical; benchmark
  attribution stays clean.
- **Alternatives:** Pure-rename aliases (two grammars in practice, hidden
  from the schema); aggressive op-name coercion (rejected — `insert` is
  ambiguous between before/after, and a wrong silent mapping corrupts an
  edit instead of erroring).
- **Consequences:** The first failed call still happens; its cost drops to
  one teaching round-trip. Evidence check: 330 codex/omp calls produced zero
  guesses, so aliases would only absorb behavior the teaching error corrects
  per-session anyway.

### ADR-002: One generic wrapper, not per-class messages [status: accepted]

- **Context:** Error construction could be per-failure-mode handcrafted text
  (unknown field vs unknown variant vs missing op), a generic
  append-example wrapper, or a hybrid with a `find` special case.
- **Decision:** Generic wrapper at the single op-deserialize interception
  point in `src/edit/json.rs` `lower_section`: append one canonical example
  op plus the line-addressed sentence to every op parse failure.
- **Alternatives:** Per-class arms (max teaching value, more maintenance,
  new guess patterns fall through raw until added); hybrid `find`
  special-case (rejected with it — the generic sentence already names the
  find/replace misconception).
- **Consequences:** ~10 lines; covers unknown future guesses; one test
  surface.

### ADR-003: Silent leading-`#` strip on tags [status: accepted]

- **Context:** Observed `tag: "#A0EA"` — an agent copying the tag with the
  display prefix from the `[path#TAG]` read format. The tag is the drift
  gate, where silent normalization has the worst downside if the assumption
  is wrong.
- **Decision:** Strip one leading `#` before `parse_tag`; the 4-hex gate
  runs unchanged on the stripped value. This is parsing display syntax, not
  guessing intent — a wrong tag still fails.
- **Alternatives:** Teaching error without strip (zero silent normalization
  near the gate, cost of one retry) — viable, rejected for a prefix that is
  unambiguous by construction.
- **Consequences:** Strip lives at the lowering site (`json.rs`), keeping
  `parse_tag`/`tag.rs` strict.

### ADR-004: Overlap gate for replace_text, strict gate for line ops [status: accepted]

- **Context:** The seen-lines gate rejected any `replace_text` whose resolved
  span crossed the edge of the displayed window (15 of 19 unseen-anchor
  rejections in the post-fix window; a section read of 6-41 lines with the
  match span 1-5 lines outside it). The whole matched span had to be seen even
  though the agent had clearly seen the text it was replacing.
- **Decision:** `replace_text` passes when its resolved match span OVERLAPS the
  seen set by at least one line; a span with zero seen lines is still rejected.
  Line, insert, and block ops keep the strict per-anchor rule (every anchored
  line must have been displayed). The span is resolved via the same matcher the
  apply path uses (`apply.rs match_text_span`) — the matcher is shared.
- **Consequences:** `check_seen_lines` splits ops — text swaps checked for
  overlap, the rest lowered and checked strictly. `replay_session_chain` keeps
  its own strict guard (a security net against a text-swap provenance bypass on
  the drift path) and is deliberately NOT loosened.

### ADR-005: Self-correcting rejection messages + whitespace-normalized fallback [status: accepted]

- **Context:** Two rejection classes dominated and both sent the agent guessing.
  Unseen-anchor named neither what WAS displayed nor where to re-read.
  `text to replace was not found` named neither the provenance source nor the
  cheap fix; 9 of 15 misses were `old` retyped from memory or shell output.
- **Decision (two messages):**
  1. Unseen-anchor names the displayed ranges and the exact re-read:
     `line {N} was never displayed under this tag (displayed: {A-B, C-D}).
     Re-read {path}#{lo}-{hi} to cover line {N}.` `{lo}-{hi}` is the smallest
     span joining N to the nearest displayed range, capped at 60 lines by
     trimming the far (range) side so the anchor stays covered.
  2. Not-found teaches provenance: `copy old verbatim from the numbered lines
     of [{path}#{TAG}] — do not retype it from memory or shell output`.
- **Decision (fallback):** When the exact `old` misses, retry with runs of
  spaces/tabs collapsed and leading/trailing whitespace ignored on both sides.
  Exactly one normalized match applies `new` over the ORIGINAL span and appends
  `(matched with whitespace normalization)` to the section status line. Zero →
  the not-found message above; more than one → the existing not-unique error.
  Exact-ambiguous and empty-`old` paths are untouched. `replace_all` (#175)
  stays out of scope.
- **Consequences:** The `normalized` flag threads `apply.rs` →
  `ApplyResult.normalized_swap` → the write status line. Prompts are unchanged;
  the error text is the teaching channel (never truncated by the host).

### ADR-006: All-sections-failed surfaces MCP isError [status: accepted]

- **Context:** Every per-section rejection returned `isError: false` with the
  error rendered inside the text body, so dashboards keyed on the MCP error
  flag counted zero write failures.
- **Decision:** `tool_write` returns `Err` (→ `isError: true`) when EVERY
  section in the call failed; a mixed call keeps `isError: false` with
  per-section `error:` lines and every section block present.
- **Consequences:** `apply_section` returns `(block, is_error)`; `tool_write`
  ORs the successes. Single-section rejections (the common shape) now surface as
  errors — the many `tool_write(...).expect(...)` write tests that exercised a
  sole rejected section moved to `.expect_err(...)`.


### ADR-007: Review amendments to ADR-004/005 (PR #232 /age pass) [status: accepted]

- **Context:** A five-lens review of the ADR-004..006 implementation found one
  blocker and two highs in the new code: (1) the gate derived the content-end
  line from `end - 1`, which is not a UTF-8 char boundary when `old` ends in a
  multi-byte char — `text[..end-1]` panicked the whole `tool_write` call after
  earlier sections had already been written; (2) the normalized fallback
  counted matches with `match_indices` (non-overlapping), so a self-overlapping
  needle passed the uniqueness guard — the same fail-open the exact path had
  already fixed; (3) the not-found teaching text was wired only on the no-drift
  path.
- **Decisions:**
  1. Gate and apply attribute a matched span to lines by *different* rules on
     purpose: the gate uses the **content span** (`apply.rs content_line_span`,
     char-safe: last char start in `start..end`) so a trailing `\n` never counts
     the phantom next line; apply keeps the **covering span** (exclusive `end`)
     because run coalescing relies on it. Only the matcher is shared.
  2. Normalized-match rules: ambiguity is overlap-aware (same probe as the exact
     path); a multi-line normalized match additionally requires each interior
     line's leading whitespace to be byte-equal between the file span and `old`
     (only trailing/intra-line runs and the first line's indent may differ), so
     the fallback cannot splice the model's indentation into a
     whitespace-significant file; a whitespace run immediately before a newline
     or end-of-string normalizes to nothing, not to one space.
  3. One producer of the not-found teaching text covers both
     `EditError::Apply(TextUnmatched)` and the drift path's
     `MismatchError::TextMatch { source: TextUnmatched }`; the `[path#TAG]`
     header comes from `tag::format_header`.
  4. Unseen-anchor `displayed:` lists at most the 4 ranges nearest the anchor,
     nearest first, then `… +K more`; the re-read window keeps the anchor covered
     even when the anchor region itself exceeds the 60-line cap.
  5. `lower_ops` returns a named `Lowered { line_ops, file_op, normalized }`;
     `normalized_swap` is threaded through `try_recover` and `commit_file_op`, so
     the status suffix also fires on the drift path and on `move_file` +
     `replace_text` sections. `apply_section` returns `Result<String, String>`
     (supersedes the ADR-006 consequence line that named `(block, is_error)`).
- **Deferred (not decided here):** a minimum seen-fraction for large tolerant
  spans (would revise ADR-004); single-pass lowering shared by gate and apply
  (needs an intermediate carrying byte spans; pre-existing double parse for
  block anchors); a per-op normalized-`old` list on the status line.



### ADR-008: Bounded write output preserves displayed-line provenance [status: accepted]

- **Context:** A one-line edit currently returns the complete file. Repeated large-file edits consume the caller's context despite bounded source reads.
- **Decision:** Bound source and optional diff output across the write response. Preserve each section's status, path, fresh tag, and reread guidance.
- **Safety:** Keep complete snapshot text for stale-edit recovery. For existing files, mark only source lines present in the final response as displayed.
- **Creation:** Create-file content is agent-authored. The returned create tag authorizes those authored lines without another read. Tests enforce this rule.
- **Alternatives:** Raising agent context limits masks amplification. Truncating after snapshot registration falsely authorizes hidden lines.
- **Verification:** Test large files, long lines, multiple sections, partial failures, stale recovery, and `diff: true`.[^bounded-write]

[^bounded-write]: `src/mcp/tools/write.rs`; `src/edit/snapshots.rs`; `src/edit/recovery.rs`. Decision: September 19, 2026, after the measured coder context-loss investigation. The implementation PR records gate results and release status.

### ADR-009: Multi-match unseen rejections name one unblocking call (amends ADR-005) [status: accepted]

- **Context:** `rewrite` and `replace_text all` touch many matches that can sit far apart. The ADR-005 re-read caps at 60 lines, so agents re-read in steps or read the whole file. Sonnet 5.5 benchmark runs showed this on `render/render_test.go` (`count: 43`).
- **Decision:**
  1. `replace_text all` and `rewrite` name one batched read: `tilth_read paths {paths} shows every match range in the current file; use the tag returned by that read when retrying.`
  2. For `replace_text all`, each range is the first line of one unseen occurrence. One displayed line passes the overlap gate.
  3. For `rewrite`, each range is one full match span. The full-span gate needs every line of each match.
  4. Ranges within 3 lines merge. The closest neighbours then merge until at most 20 ranges remain, because `tilth_read` takes at most 20 paths. A test in `src/mcp/tools/definitions.rs` pins the two caps together.
  5. Single `replace_text` and line ops keep the ADR-005 message unchanged.
- **Alternatives:**
  1. A literal `{query, glob}` search hint for `replace_text all`. `tilth_search` routes queries automatically, so the hinted search can hide occurrences in five ways:
     - A same-file definition takes the symbol route, which shows only the definition.
     - A word-bounded symbol match misses substrings.
     - Metacharacters take the regex route.
     - Matches inside functions show outline context with no tag.
     - Test files go to the compact facet.

     In each case the retry loops.
  2. A structural `tilth_search` hint for `rewrite`. The structural walker skips some files that a glob names: `build`, `vendor`, `dist` and `target` directories, root names with glob metacharacters, and ignored or large files. The search then shows no match, and the retry loops.
  3. A single-file search cap of 1000 matches. It does not fix routing. It costs about 20x tokens on routine single-file searches. It overflows the 24k budget for long lines.
  4. One covering re-read from the first to the last unseen occurrence. An over-budget section read shows few lines but marks the whole range as seen, so the retry applies to unseen lines.
- **Consequences:** Small ranges keep the hinted read under its budget. The gate checks one op at a time, so a section with two multi-match ops can need two hinted calls. Each call shrinks the unseen set, so the retries converge.
- **Open issue:** A truncated `tilth_read` section records its whole requested range as seen (paulnsorensen/tilth#305). ADR-008 fixes the same class of bug for write output. Until #305 lands, more than 20 spread-out matches can merge into wide ranges that truncate.
- **Verification:** `tests/mcp_v2/test_unseen_match_hint.py` runs the first read, the hinted read, and the retry in one session. The retry must apply, and the hinted read must not truncate. One case documents the #305 residual as an expected failure.[^multi-match]

[^multi-match]: `src/edit/recovery.rs` (`merge_ranges`, `MAX_READ_RANGES`); `src/edit/mismatch.rs` (`unseen_message`). Decision: October 3, 2026, after three /age passes on `feat/edit-efficiency` that reproduced each rejected design against the built binary.


### ADR-010: Tag fill-in and exact-match exemption (amends ADR-004 and ADR-009) [status: accepted]

- **Approval:** The user approved this guard change on 2026-11-04.
- **Context:** A benchmark trajectory diagnosis (wiki page `stock-ab-trajectory-diagnosis.md`, commit `d615858a`, causes 5 and 8) found two costly rejections.
  - Cause 8 was the most common write error, 111 times in 162 cells. A tagless `replace_text` or `rewrite` got `needs a tag; this session already displayed [path#TAG]`. The server knew the tag, so the retry only sent it back.
  - Cause 5: a `replace_text` whose `old` matched exactly once got an unseen-line rejection. The exact match proves that the agent holds that text.
- **Decision:**
  1. Tag fill-in. A tagless section with `replace_text` or `rewrite` takes the live tag when this session holds a snapshot whose text equals the live file (`fill_live_tag` in `src/mcp/tools/write.rs`). The section then runs the tagged path, with the gate and the seen-line carry-forward. With no such snapshot, the rejection stays.
  2. Exact-match exemption. On the no-drift path (`gated_apply` in `src/edit/recovery.rs`), a single `replace_text` (no `all`, no `count`) whose `old` matches byte-exactly once skips the overlap check.
  3. These stay gated: `all`/`count`, `rewrite`, a whitespace-normalized match, line, insert, and block ops. The drift path (`check_seen_lines` in `recover_edit`, and `replay_session_chain`) does not change.
- **Consequences:** The exemption marks only the lines that the edit wrote as seen. Lines next to the match stay unseen. Tests that used an exact `replace_text` to prove an unseen-line rejection now use line ops. The `tilth_write` description now limits "every match must be on shown lines" to `all`/`count` matches.
- **Verification:** `tests/mcp_v2/test_write_tag_fill_exact_match.py` and the `gated_apply_*` unit tests in `src/edit/recovery.rs`.



### ADR-011: Seen lines persist per checkout across tilth processes [status: accepted]

- **Context:** The seen-lines gate kept its record only in the MCP server process. `claude --resume` starts a new `tilth --mcp` process, so the gate forgot earlier reads, although the agent still had their text and tags. In the stock A/B chain benchmark, each chain sub-task ran as a new `claude -p --resume` process. Sub-task 3 displayed `glue.rs` lines 11-300 and 310-360. The edit sub-task then wrote line 166 and got `displayed: 58, 1-15`. It re-read 352 lines in `mode=full` and retried. Rejections were 0.33-0.67 per chain cell and 0.07-0.14 per single-session cell.
- **Decision:** This is a guard change. The user approved it on 2026-11-04.
  1. `src/edit/seen_store.rs` saves one entry per file version: the normalized path key, the whole-file tag, a 64-bit xxHash of the content, and the displayed ranges. It never saves file text.
  2. The scope is the checkout: the first ancestor of the call's `cwd` with a `.git` entry, else the `cwd`. The file is `$XDG_STATE_HOME/tilth/seen/<xxh64 of root>.json` (default `~/.local/state`), outside the repository.
  3. `dispatch_tool` saves the seen lines that each call recorded: reads, searches, and write receipts. A save is atomic (a 0600 temp file, then a rename; the directory is 0700).
  4. `resolve_edit` adds saved lines to the gate only when the live tag equals the section tag and the live content digest equals the saved digest. This applies with or without a session snapshot for the tag. `carried_seen_lines` carries the saved lines to the edited version.
  5. Bounds: 256 entries per checkout, 4 versions per path, 256 ranges per entry, and 128 state files. The least recently saved drop first. A missing, corrupt, oversized, or unwritable state file reads as empty, and the tool call continues.
- **Alternatives:**
  1. Use saved lines only when the session has no snapshot for the tag. Rejected: the benchmark process had its own snapshot for the tag, with lines 1-15 and 58.
  2. Scope the record by the git root of the edited file. Rejected: a process in another checkout could then use the record through an absolute path.
  3. Save file text, so that 3-way recovery also works across processes. Rejected: it puts source text on disk outside the repository.
- **Consequences:**
  - Two agents in one checkout share the record. An agent can edit lines that only the other agent displayed, when the content is identical. Runs that reuse one checkout path share it too. Give each benchmark run its own `XDG_STATE_HOME` for clean attribution.
  - The drift path (`recover_edit`) does not use saved lines, because a 3-way merge needs base text.
  - The unchanged-reread stub does not use saved lines, so a resumed re-read still returns text.
  - Unit tests get a disabled store (`SeenStore::from_env` under `cfg(test)`). The `tests/mcp_v2` harness and the BDD session set a throwaway `XDG_STATE_HOME`.
- **Verification:** `tests/mcp_v2/test_seen_persist.py` fails on `aec0efd4` (4 failures, 1 error) and passes with the change. Unit tests are in `src/edit/seen_store.rs`, `src/edit/snapshots.rs`, and `src/mcp/tools/write.rs`.[^seen-persist]

[^seen-persist]: `src/edit/seen_store.rs`; `src/session.rs` (`persist_seen`, `persisted_seen`); `src/mcp/mod.rs` (`dispatch_tool`); `src/mcp/tools/write.rs` (`resolve_edit`, `carried_seen_lines`). Decision: November 4, 2026, from the stock A/B trajectory diagnosis, cause 3.
