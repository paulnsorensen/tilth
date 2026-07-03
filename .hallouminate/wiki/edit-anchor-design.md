# Edit-anchor design: per-line hash vs whole-file tag (tilth vs oh-my-pi)

Why tilth anchors edits with a per-line content hash, what that costs in tokens, and the open strategic question of whether to switch to oh-my-pi's whole-file-tag model. Full comparison in `.cheese/notes/oh-my-pi-comparison.md` (§3, §9); hash research in `.cheese/research/{hash-anchor-collision-design,fnv1a-line-hash-anchor,xxhash32-vs-fnv1a-truncation}/` (local, gitignored). Related: [[mcp-cwd-root-binding]].

## Two opposite structural bets

tilth and oh-my-pi (the coding agent tilth forked its ideas from) did not pick different points on one dial; they made opposite bets on the same problem.

- **tilth — many cheap per-line fingerprints.** One `FNV-1a → 12-bit` hash per line, rendered `N:hash|content` (`src/format.rs:135-144`). Buys **localized staleness** (an unrelated edit elsewhere does not invalidate a pending anchor), **snapshot-free relocation recovery** (a moved-but-unchanged block is rescued by exact match against the fresh file, no session snapshot needed), and per-line addressing. Costs a recurring per-line token tax (below).
- **oh-my-pi — one whole-file fingerprint.** A single `xxHash32 → 16-bit` tag for the entire file (`[path#TAG]`). Detects any drift globally and recovers richly (3-way merge against a cached snapshot), but any edit anywhere invalidates the tag and recovery needs a session snapshot to exist.

The tracker frames tilth's localized-staleness and snapshot-free recovery as strengths to **keep** and give **back** to oh-my-pi, not surrender.

## The hash-quality bug: FNV low-bit masking is the wrong truncation

`line_hash` (`src/format.rs:122-131`) truncates with `& 0xFFF`, masking the **low** 12 bits. That is the documented wrong way for FNV-1a: its low bits are structurally the least-mixed (the low bit is the XOR of every input byte's low bit, independent of higher bits). The FNV author's site and the IETF FNV draft prescribe **xor-folding** instead — fold the excess high bits down and XOR into the kept bits. `<certain>` (research §9.1).

xxHash32 is safe under low-bit masking only because it has an avalanche finalizer that makes every bit-range uniform — that is the load-bearing difference between the two designs, not the bit width. `<certain>` (research §9.2).

Fix options, cheapest first: (a) xor-fold the FNV output to 12 bits, ~2 lines, keeps the hash and the 3-hex display; (c) switch to xxHash32 + mask. Option (a) is the minimal correct fix.

## Which failure the width defends against

Two distinct risks — the scary-looking one is not the governing one (research §9.3):

- **Birthday collision** — "do any two lines share a bucket." 12-bit hits 50% at just n=76 lines. But this **overstates** tilth's risk: the anchor is `line:hash`, not `hash` alone, so a bucket collision only bites if the model *also* fumbles the line number and the wrong line happens to match. Compound, rare.
- **Flat false-accept-of-stale** — "a changed line re-hashes to the stale anchor's value, so the edit is accepted against content never re-verified." This is **1/4096** (tilth 12-bit) vs **1/65536** (oh-my-pi 16-bit), independent of file size. This is the honest number to design against.

## The floor is set by width, not by FNV-vs-xxHash

Key insight for any future hash change: once you xor-fold, **folded-FNV-16 == xxHash-16 == 1/65536**. xxHash's only residual edge is marginally stronger avalanche. So the lever that actually moves the false-accept floor is **hex width** (3 vs 4 hex), and 16 bits is reachable with folded-FNV without adding a dependency. `<certain>` on the arithmetic; `<speculating>` that folded-FNV-16 is empirically indistinguishable from xxHash-16 for source lines — no direct study measured it.

Detail: `32 → 16` is a single clean fold (`(h >> 16) ^ (h & 0xFFFF)`, no leftover bits); `32 → 12` leaves 8 unfolded high bits. Folding to 16 is cleaner than to 12.

## The token cost — the crux of the strategic question

Measured this session with `tiktoken` `o200k_base` (a proxy for Claude's tokenizer; ratios robust, absolutes ±) over 5 real tilth files, 8092 lines:

| rendering | tokens | vs oh-my-pi |
|---|---|---|
| raw content | 71,286 | — |
| oh-my-pi (one file tag + numbered lines) | 91,609 | baseline |
| tilth 3-hex (`N:hash\|`) | 114,104 | **+22,495 (+24.6%)** |
| tilth 4-hex | 120,720 | +29,111 (+31.8%) |

- Per-line hash tax over the whole-file-tag rendering: **~2.78 tok/line (3-hex)**, ~3.60 (4-hex).
- **4-hex marginal over 3-hex is only +0.82 tok/line** — so token cost is *not* a valid argument against widening to 4 hex. The real reasons to stay at 3-hex are eyeball-scannability and haiku-benchmark format sensitivity.
- oh-my-pi's whole-file tag is **O(1)** — one tag regardless of file size; tilth's tax is **O(lines)**.

Why ~2.78 tokens for a 4-char `a3f|` prefix: the hex string does not merge with neighbours and `|` is its own token, so the prefix *fragments* the line's tokenization beyond its literal char count. `<certain>` on the mechanism; magnitude is tokenizer-specific.

## No double-read, but every read is taxed

A natural worry is "you must read before you edit, so do you read twice — once to view, once for hashes?" No. `edit_mode` is a **server-global startup flag** (`src/mcp/mod.rs:120-126`, read at `:294`/`:355`), not a per-call toggle. Its doc: *"when edit_mode is true, exposes tilth_write AND switches tilth_read to hashline format."* `tilth_write` only exists when `edit_mode` is true (`:386`). So in any edit-capable deployment every `tilth_read` (and `tilth_search`'s expanded bodies) already comes back hashlined — you read once, you have the anchors, you edit. `<certain>`.

The flip side sharpens the cost: because the flag is global, **every** read in an editing session pays the ~25% tax, including pure-comprehension reads that never lead to an edit. There is no hash-free read in an edit-mode server (`read_ranges`, signature mode all take `edit_mode` — `src/read/mod.rs:228-232`, `:540`). oh-my-pi's O(1) tag stays cheap on comprehension reads.

## Open strategic question (for a future /mold)

If tilth's design point is token efficiency for AI agents, per-line anchoring works *against* that on every edit-mode read (~25% tax), while oh-my-pi's whole-file tag avoids it. The counterweight is tilth's localized staleness + snapshot-free recovery. This is unresolved and worth a dedicated `/mold`: keep per-line (and pursue the scoped hash-hygiene fixes: xor-fold + a 32-bit range checksum for multi-line interiors), or switch to the whole-file-tag model and rework the edit protocol. The scoped hash-hygiene spec is on hold pending that decision — see `.cheese/notes/oh-my-pi-steal-shortlist.md` and the wheypoint note that supersedes it.

## Source referents

- `src/format.rs:122-131` — `line_hash`, the `& 0xFFF` low-bit mask (the bug).
- `src/format.rs:135-144` — `hashlines`, the `N:hash|content` render.
- `src/edit.rs:14-20` — `Edit` struct (fields `start_line/start_hash/end_line/end_hash/content`).
- `src/edit.rs:142-171` — verify loop: checks start + end hash only; multi-line interior unverified.
- `src/read/mod.rs:228-232` — `edit_mode`-gated hashline emission.
- `src/mcp/mod.rs:120-126` — `edit_mode` server-global flag semantics.
