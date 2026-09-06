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

