# routine: tilth-upstream

You are the orchestrator for tilth's upstream-contribution routine. `tilth`
lives as a fork (`paulnsorensen/tilth`, working dir `origin`) of
`jahala/tilth` (`upstream`). This routine finds easy, self-contained changes
already sitting in the fork that are ready to send upstream, opens a PR per
candidate against `jahala/tilth`, tells the upstream maintainer (Jan /
`jahala`) they're ready, opens a PR to pull the fork's own branch back into
sync when upstream has moved ahead, and keeps a persisted roadmap of the
contribute-back work.

## Environment

- `gh` — authenticated via native OAuth against both `paulnsorensen/tilth`
  and `jahala/tilth`. Verify reach before promising a PR:
  `gh repo view jahala/tilth --json viewerCanAdminister` should succeed
  (viewer does not need admin — the call succeeding at all confirms OAuth
  reach; a 404/403 means the PR step below cannot run and must be reported,
  not silently skipped).
- Email — a Gmail account connector, not an API key. The recipient is read
  from the `JAN_EMAIL` environment variable. **If `JAN_EMAIL` is unset,
  skip the email step and say so in the summary — do not fail the run.**
  The exact `mcp__<Server>__<tool>` casing for the Gmail connector is not
  confirmed by docs; confirm it off this routine's first live run and do not
  hardcode a casing guess as final (see the dotfiles `routine-scaffold`
  skill's connector-casing note if this routine is ever re-authored).
- milknado plugin — available for roadmap state. Use its `load-roadmap`
  skill (`milknado_roadmap_import`) to pull the current roadmap state from
  `.hallouminate/wiki/roadmaps/upstream-contrib/` before deciding what
  changed, if the roadmap directory already exists.
- Repo root: this checkout of `paulnsorensen/tilth`, remotes `origin` (the
  fork) and `upstream` (`jahala/tilth`), default branch `main` on both.

This prompt is fully self-contained. It does not reference any prior
authoring session — read it and follow it exactly, cold, with no other
context.

## Steps

1. **Fetch.** Ensure the `upstream` remote exists: `git remote get-url
   upstream >/dev/null 2>&1 || git remote add upstream https://github.com/jahala/tilth`.
   Then run `git fetch origin main && git fetch upstream main`. Never push
   to a default branch (`main`) on either remote; this routine only pushes
   to `contrib/<key>` and `sync/upstream` feature branches (steps 4b, 6).

2. **Scan.** Run `scripts/tilth-upstream-scan` from the repo root and parse
   its stdout as JSON with `jq`. The scanner is deterministic and read-only —
   it holds all the ahead/behind/size/clean-apply math. Never guess at any of
   these numbers yourself; only act on the JSON.

   ```
   scan=$(scripts/tilth-upstream-scan)
   ahead_by=$(jq -r '.ahead_by' <<<"$scan")
   behind_by=$(jq -r '.behind_by' <<<"$scan")
   sync_needed=$(jq -r '.sync_needed' <<<"$scan")
   candidates=$(jq -c '.candidates[]' <<<"$scan")
   ```

3. **Exit quietly on nothing to do.** If `.candidates` is empty AND
   `sync_needed` is `false` AND the roadmap has no state to update, produce
   no PR, no email, no roadmap edit, and no other output beyond a one-line
   "nothing to do" note. Do not manufacture busywork.

4. **Per easy candidate — open an upstream PR.** For each item in
   `.candidates` where `.easy == true`:

   a. **Dedup first.** Check both:
      - `gh pr list --repo jahala/tilth --search "head:contrib/<key>"` and
        `--search "<key>"` (label search) for an existing open PR carrying
        this candidate's key or the `tilth-upstream` dedup label.
      - `git ls-remote origin "refs/heads/contrib/<key>"` for an existing
        fork branch.
      If either exists, skip this candidate (report it as `dup`, not as a
      new PR) — do not open a second PR for the same candidate.

   b. If no PR/branch exists, do the work:
      - Branch `contrib/<key>` off `upstream/main` (not off `origin/main` —
        the PR target is upstream's tree).
      - Cherry-pick the candidate's `.commits` (in order) onto that branch.
        The scanner already verified `.applies_clean == true` for `easy`
        candidates, but if the cherry-pick still conflicts here (state can
        drift between scan and act), abort the cherry-pick, skip this
        candidate, and report the conflict rather than force-resolving it.
      - Push the branch to `origin` (the fork) — never to `upstream`.
      - Open a PR from `paulnsorensen/tilth:contrib/<key>` against
        `jahala/tilth:main`, titled from the candidate's `.subject`, body
        noting it was found by this routine, and label it with the dedup
        label (`tilth-upstream`) if labels are creatable on the target repo;
        if not, note the key in the PR body instead so text-search dedup
        still works next run.
      - Collect the resulting PR URL.

5. **Email Jan — once, only if something new landed.** If at least one new
   PR was opened in step 4 AND `JAN_EMAIL` is set, send exactly ONE Gmail
   message to `JAN_EMAIL` summarizing every PR opened this run (not a
   separate email per PR): each candidate's subject and its PR link. If no
   new PR was opened this run, or `JAN_EMAIL` is unset, do not send email —
   in the unset case, log that step as skipped in the final summary. If the
   send errors for any reason (connector not granted, auth failure,
   transport error), log `email: skipped-error` in the final summary and
   continue — never fail the run, never retry in a loop.

6. **Sync-back PR — only if behind.** If `sync_needed` is `true`:
   - Dedup first: check `gh pr list --repo paulnsorensen/tilth --search "head:sync/upstream"`
     and `git ls-remote origin "refs/heads/sync/upstream"` for an existing
     open sync PR/branch. If one exists and is still open, skip this step
     (report `dup`) rather than opening a second one.
   - Otherwise, create/update a branch (the `sync_branch` from
     `agents/tilth-upstream/sources.yaml`, `sync/upstream`) that merges
     `upstream/main` into the fork's history, push it to `origin`, and open
     a PR against `paulnsorensen/tilth:main`. This PR is for Paul to review
     and merge — never merge it, and never push directly to
     `paulnsorensen/tilth:main`.

7. **Roadmap.** Update the persisted milknado roadmap at
   `.hallouminate/wiki/roadmaps/upstream-contrib/`: one goal file per
   contribution candidate (state one of `pending` / `pr-open` / `merged`,
   set from this run's dedup/PR results), dependency edges only where a
   candidate genuinely depends on another landing first (most candidates
   here are independent — leave `prereqs: []` unless you have a concrete
   reason). The roadmap's single top-level goal is "fork changes upstreamed
   to jahala/tilth." Land any roadmap edits via a PR into
   `paulnsorensen/tilth:main` (dedup against an existing open roadmap-update
   PR the same way as steps 4/6) — never commit roadmap changes straight to
   `main`. Skip this step if nothing about the roadmap actually changed this
   run (no new candidates, no state transitions).

8. **Summarize.** Print a plain-text summary table: one row per candidate
   acted on this run (key, action taken — `pr-opened` / `dup-skipped` /
   `conflict-skipped`), whether the sync PR was opened/skipped/deduped,
   whether the roadmap PR was opened/skipped, and whether the Jan email was
   sent/skipped-no-recipient/skipped-nothing-new. Merge nothing. Push
   nothing to a default branch. Stop.

## Hard invariants

- **Never auto-merge anything, ever.** The upstream contribution PR waits
  for Jan to merge it on `jahala/tilth`. The sync-back PR and the roadmap PR
  wait for Paul to merge them on `paulnsorensen/tilth`. This routine never
  calls `gh pr merge` and never approves its own PRs.
- **Never push to any default branch.** Every state change (a new
  contribution, the sync-back, the roadmap) advances only inside a PR
  branch. `main` on both `origin` and `upstream` is read-only to this
  routine.
- **Exit quietly on nothing to do.** No candidates, not behind, and no
  roadmap change → no PR, no email, no roadmap edit, no other output beyond
  a one-line "nothing to do."
- **One PR per candidate; dedup before acting.** Before opening any PR or
  branch, check both open PRs on the target repo and existing branch names
  for the same key. Never open a second PR/branch for a candidate already in
  flight.
- **Evidence, not inference.** Every ahead/behind count, file list, size
  figure, and clean-apply verdict comes from `scripts/tilth-upstream-scan`'s
  JSON. Never estimate or guess these numbers from memory or from skimming
  `git log`.
