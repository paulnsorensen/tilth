#!/usr/bin/env python3
"""Evolve tilth: a GEPA search over instruction texts and ``src/**`` patches, scored by grader correctness.

    python3 benchmark/evolve/cli.py --panel benchmark/panels/gepa-v1.json --seed-sha SHA \\
        --max-usd 200 --max-metric-calls 60 [--base-branch main] [--model haiku]

Every candidate is a local commit on the seed; the winner, when it is not the
seed, is pushed as one branch and opened as one draft PR. Nothing is merged.
"""

import argparse
import math
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
if not __package__:
    # Run as a script: resolve bare harness imports such as ``config`` against benchmark/.
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != _PACKAGE_DIR]
    sys.path.insert(0, str(_PACKAGE_DIR.parent))

import baselines  # noqa: E402
import panels  # noqa: E402
import run  # noqa: E402
from config import REPO_ROOT  # noqa: E402
from evolve.candidate import SeedError, read_seed  # noqa: E402
from evolve.finish import GitHubPRClient, PRClient  # noqa: E402
from evolve.gitops import GitError, git  # noqa: E402
from evolve.loop import Evolution, EvolveError, Settings  # noqa: E402
from judge import core as judge  # noqa: E402
from spend import SpendLedger  # noqa: E402


def default_just_check(worktree: Path) -> tuple[bool, str]:
    """Run ``just check`` in an allowlisted env, with ``<worktree>/target`` linked to the shared candidate target dir.

    A symlink, not CARGO_TARGET_DIR: tests/mcp_v2 reads ``<repo>/target/debug/tilth``, and the root .gitignore
    ``/target`` matches a symlink.
    """
    target = Path(worktree) / "target"
    if not target.is_symlink() and not target.exists():
        shared = run.candidate_target_dir()
        shared.mkdir(parents=True, exist_ok=True)
        target.symlink_to(shared, target_is_directory=True)
    completed = subprocess.run(["just", "check"], cwd=worktree, capture_output=True, text=True,
                               env=run.build_tool_env())
    return completed.returncode == 0, completed.stdout + completed.stderr


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--panel", type=Path, required=True, help="pre-registered panel file")
    parser.add_argument("--max-usd", type=float, help="required: one spend ceiling for every paid call in the run")
    parser.add_argument("--max-metric-calls", type=int, required=True,
                        help="GEPA budget: evaluator calls, one per (candidate, dev task)")
    parser.add_argument("--seed-sha", required=True, help="the tilth commit every candidate builds on")
    parser.add_argument("--base-branch", default="main", help="the branch --seed-sha came from (default: main)")
    parser.add_argument("--model", default="haiku", choices=sorted(run.MODELS), help="agent model for every cell")
    parser.add_argument("--reruns", type=int, default=3, help="re-runs R before a candidate is accepted (default 3)")
    parser.add_argument("--plateau", type=int, default=5,
                        help="stop after P paid-tier candidates leave the best dev mean unimproved (default 5)")
    parser.add_argument("--cell-estimate-usd", type=float, default=run.DEFAULT_MAX_BUDGET_USD,
                        help="estimate for a call with no stored or earlier cost")
    parser.add_argument("--refreeze-baselines", action="store_true",
                        help="buy drifted baseline cells again under their new key")
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def build(argv: list[str] | None = None, *,
          judge_factory: Callable[[SpendLedger, float], judge.Judge] | None = None,
          pr_client: PRClient | None = None, just_check: Callable[[Path], tuple[bool, str]] | None = None,
          spawn: Callable | None = None) -> Evolution | int:
    """Validate the flags and build the run, or return an exit code; no model call happens here."""
    args = _parser().parse_args(argv)
    if args.max_usd is None:
        return _error("--max-usd is required: it bounds every cell, judge, reflection, and proposer call")
    for flag, value in (("--max-usd", args.max_usd), ("--cell-estimate-usd", args.cell_estimate_usd)):
        if not math.isfinite(value) or value <= 0:
            return _error(f"{flag} must be a positive finite number")
    if args.max_metric_calls < 1 or args.reruns < 0 or args.plateau < 1:
        return _error("--max-metric-calls and --plateau must be at least 1, --reruns at least 0")
    try:
        run.guard_claude_auth(os.environ)
    except run.ClaudeAuthError as error:
        return _error(str(error))
    repo = args.repo.resolve()
    try:
        seed_sha = git("rev-parse", "--verify", f"{args.seed_sha}^{{commit}}", cwd=repo).strip()
        seed = read_seed(repo, seed_sha)
    except (GitError, SeedError) as error:
        return _error(str(error))
    if subprocess.run(["git", "merge-base", "--is-ancestor", seed_sha, args.base_branch], cwd=repo,
                      capture_output=True).returncode != 0:
        return _error(f"--seed-sha {args.seed_sha} is not an ancestor of --base-branch {args.base_branch}")
    try:
        panel = panels.load_panel(args.panel, store_path=run.RESULTS_DIR / baselines.STORE_FILENAME)
        panel.register(run.TASKS)
    except panels.PanelError as error:
        return _error(str(error))
    ledger = SpendLedger(args.max_usd)
    factory = judge_factory or (lambda shared, floor: judge.Judge(shared, cell_estimate_usd=floor))
    settings = Settings(
        run_id=args.run_id, repo=repo, seed_sha=seed_sha, base_branch=args.base_branch, model=args.model,
        reruns=args.reruns, plateau=args.plateau, max_usd=args.max_usd, max_metric_calls=args.max_metric_calls,
        cell_estimate_usd=args.cell_estimate_usd, refreeze_baselines=args.refreeze_baselines, panel_path=args.panel,
    )
    return Evolution(settings, panel=panel, ledger=ledger, judge_=factory(ledger, args.cell_estimate_usd), seed=seed,
                     pr_client=pr_client or GitHubPRClient(repo), just_check=just_check or default_just_check,
                     spawn=spawn or subprocess.run)


def main(argv: list[str] | None = None, **deps) -> int:
    evolution = build(argv, **deps)
    if isinstance(evolution, int):
        return evolution
    try:
        return evolution.run()
    except (EvolveError, run.ClaudeAuthError) as error:
        evolution.log(f"error: {error}")
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
