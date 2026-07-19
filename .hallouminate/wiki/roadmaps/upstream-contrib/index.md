---
created: 2026-07-19
---
# Upstream contribution roadmap — fork changes upstreamed to jahala/tilth

`paulnsorensen/tilth` (this fork) has accumulated 103 commits ahead of
`jahala/tilth` (upstream) with no systematic path back. The top-level goal
this roadmap tracks is: **every self-contained, easy improvement sitting in
the fork gets a PR against `jahala/tilth`, and the fork stays reconciled
against upstream's own progress.**

This roadmap is populated and kept current by the `tilth-upstream` cloud
routine (`agents/tilth-upstream/routine.md`), which runs weekly. Each run's
scanner output (`scripts/tilth-upstream-scan`) becomes zero or more goal
nodes below — one per contribution candidate the scanner classifies `easy`.
A goal's state (`pending` / `pr-open` / `merged`) lives in that goal file's
own frontmatter/body and is advanced only by the routine, from PR state it
observed via `gh`, never guessed.

Most candidates are independent single commits with no dependency on each
other — prereqs stay `[]` unless a candidate genuinely can't land before
another one does (e.g. it touches a file another candidate also renames).
The sync-back track (pulling `upstream/main` into the fork) is not modeled
as a goal here — it's a standing operational task the routine handles
directly each run, not a one-time contribution to close out.

No goal nodes are seeded yet — this index establishes the roadmap's
structure and conventions ahead of the routine's first live run, which adds
one goal file per `easy` candidate the scanner finds. See
`agents/tilth-upstream/sources.yaml` for the "easy" thresholds and the
upstreamable/exclude path globs that gate what can ever become a candidate.

<!-- HALLOUMINATE:INDEX-START -->
<!-- HALLOUMINATE:INDEX-END -->
