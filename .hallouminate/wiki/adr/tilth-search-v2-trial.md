---
status: trusted
last_verified: 2026-08-16
confidence: high
sources:
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-trial.md
  - /home/paul/.local/share/cheese/paulnsorensen-tilth/specs/tilth-search-v2-roadmap.md
  - .cheese/specs/tilth-api-adoption.md
---
# ADRs — search-v2 trial slice session decisions (slug: tilth-search-v2-trial)

Session decisions from the 2026-08-16 mold that turned the search-v2 roadmap (ADRs `tilth-search-v2-roadmap-001..005`) into the approved implementation spec `tilth-search-v2-trial`. These settle what the roadmap deliberately left open; they do not re-decide F1–F15.

## ADR-001 — One session covers the full trial slice (C1–C6 staging)

The implementation spec spans baseline machinery, telemetry, spike, index engine, v2 engine, and the trial registry in one cook session. The telemetry-gated cutover (C6-final + C7) and Lane B (#186/#187) stay out — they have their own approval cycles per roadmap ADR-005. Rejected: wave-1-only (too small for the intent) and bundling Lane B (violates the independent-lane mandate).

## ADR-002 — Trial surface selected by `--search-surface v1|v2|both` CLI flag

Follows the `--edit` precedent (`src/main.rs:48-53`); default `v1` until the frozen graduation manifest authorizes the trial flip (one visible commit, follow-up F003). Matched benchmark cells pass explicit values via MCP config args. Rejected: env var (less discoverable), manifest-presence auto-default (magic, and matched cells need the flag anyway).

## ADR-003 — Redb spike is an in-cook gate with a partial halt

The spike (`spikes/redb-deps`, self-contained crate run via `cargo --manifest-path`; the repo is a single package, no workspace) runs first in the index lane. Failure halts only `deps-index-engine`; router/envelope and surface curds land cold-partial — roadmap F11 makes core search correct with zero index. Rejected: pre-spec prototype cycle (slower to approve), roadmap-literal hard halt (forfeits the session's non-index value).

## ADR-004 — Real-call telemetry is Services-owned XDG JSONL

Versioned, content-free, size-bounded JSONL under `$XDG_STATE_HOME/tilth/telemetry/`, written by a standalone `TelemetrySink` owned by `Services`; `Session`'s retired `record_savings` counters are accumulation-style precedent only. Rejected: SQLite (new dep + the multi-process writer problem F14 exists to avoid), stderr scraping (harness-dependent).

## ADR-005 — Trial window re-baselines the 13,779-char surface cap

The shipped api-adoption cap (≤13,779 chars for the `--mcp --edit` initialize+tools/list surface) cannot hold with the both-advertised trial registry. The trial cap is re-baselined with the v1-only surface asserted byte-identical, and the revert is tracked inside the graduation cutover follow-up (F003). Roadmap Risk #1 ratified the temporary schema tax. Rejected: trimming v1 descriptions deeper (contaminates the paired baseline mid-measurement), per-cell-only capping (cap loses meaning for real trial sessions).

## Baseline depth (minor)

C1 runs machinery + locally runnable haiku cells only; codex/omp floors are recorded `[BLOCKED]` (follow-up F004) and the graduation evaluator reports `blocked` until they land. The experiment-manifest machinery already exists (`benchmark/variants.py` + `prepare_experiment.py`); only the graduation format/evaluator is new.
