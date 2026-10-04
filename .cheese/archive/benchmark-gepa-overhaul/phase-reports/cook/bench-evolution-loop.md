status: ok
next: press
artifact: /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-evolution-loop.md
c5 evolve loop: benchmark/evolve package, run.run_plan + --candidate-sha, SpendLedger.reserve, paired contamination

# Cook report: bench-evolution-loop (c5)

Spec: /root/.local/share/cheese/paulnsorensen-tilth/specs/bench-evolution-loop.md (sha256 647ac602…0460, verified at bind).

## Changed
- benchmark/evolve/ (new): candidate.py (content id, seed read), gitops.py, rust.py (cfg(test) spans, byte-lock rewrite),
  materialize.py (Materializer), calls.py (StopState, PaidCalls), engine.py (only gepa importer: Stopper, Dispatcher,
  ReflectionClient, optimize), proposer.py (isolated export + scan + cumulative diff), finish.py (GitHubPRClient),
  loop.py (Evolution: preflight, baselines, evaluate/cascade, frontier, deltas, plateau, finish), cli.py (build/main).
- benchmark/run.py: CellSpec, CandidateBuild, PlanStopped, BaselineDrift, build_candidate (memoized), planned_identity,
  run_plan, --candidate-sha.
- benchmark/spend.py: SpendLedger.reserve.
- benchmark/paired.py: is_contaminated_panel_row + loader adjustment (analyze imports it).
- benchmark/requirements.txt: gepa==0.1.4. benchmark/.gitignore: results/evolve/, results/candidates/. README section.
- Tests: test_evolve.py (64), test_run_plan.py (12), c5 rows in test_invariants.py (16), test_spend/test_analyze_reporting rows.

## Checks
- RED: new tests failed on missing seams before implementation.
- cd benchmark && python3 -m pytest tests -q: 713 passed, 1 skipped (pre-existing Gin fixture skip).
- pyflakes on touched files: clean.
- python3 scripts/verify.py: exit 0.

## Seam deviations
- Spec's evolve.evaluate/cascade/finish/materialize/propose_src_patch are methods: Evolution.evaluate(candidate, example),
  Evolution.cascade(candidate) (takes the candidate, not a sha, so apply is its first stage), Evolution.finish(),
  Materializer.materialize(candidate), Proposer.propose_src_patch(candidate, records) (builds its own export).
- run_plan adds keyword args output, cell_estimate_usd, store_only; cells are run.CellSpec; stops raise run.PlanStopped.
- Candidate refs live at refs/evolve/<run-id>/<cid12> (resolvable as evolve/<run-id>/<cid12>).
- Reflection and proposer use the judge's pinned model (claude-sonnet-5); spec named no model.
- Reflection prompt carries records only (no apply/just-check tail), per "built only from these records".
- test_spend reserve row follows the defined formula (the spec's reserve-0.5 wording contradicts it).

## Risks
- certain: just check runs a full build per candidate worktree (slow, not paid).
- speculating: real proposer tool names/inputs may differ from the scanned keys (file_path/path/notebook_path, Glob pattern).
