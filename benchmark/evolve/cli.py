#!/usr/bin/env python3
"""Evolve tilth: a GEPA search over instruction texts and ``src/**`` patches, scored by cost per correct.

    python3 benchmark/evolve/cli.py --panel benchmark/panels/gepa-v1.json --seed-sha SHA \\
        --max-usd 200 --max-metric-calls 60 [--base-branch main] [--model haiku] [--replay-diagnostic N]

Every candidate is a local commit on the seed; the winner, when it is not the
seed, is pushed as one branch and opened as one draft PR. Nothing is merged.
"""

import argparse
import math
import os
import signal
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

import auth  # noqa: E402
import baselines  # noqa: E402
import panels  # noqa: E402
import run  # noqa: E402
from config import REPO_ROOT  # noqa: E402
from evolve import deepdiag, directives  # noqa: E402
from evolve.candidate import SeedError, read_seed  # noqa: E402
from evolve.finish import GitHubPRClient, PRClient  # noqa: E402
from evolve.gitops import GitError, git  # noqa: E402
from evolve.loop import Evolution, EvolveError, Settings, forbidden_terms  # noqa: E402
from evolve.materialize import PHRASE_PIN_EXEMPT_ENV  # noqa: E402
from judge import core as judge  # noqa: E402
from spend import SpendLedger  # noqa: E402

JUST_CHECK_TIMEOUT_S = 1800


def default_just_check(worktree: Path) -> tuple[bool, str]:
    """Run ``just check`` in an allowlisted env, with ``<worktree>/target`` linked to the shared candidate target dir.

    The env sets ``PHRASE_PIN_EXEMPT_ENV``, so the Rust tests skip their prompt phrase pins and a candidate can
    reword ``prompts/``. CI does not set it, so the winner PR must re-pin them (``finish.REPIN_NOTE``).

    A symlink, not CARGO_TARGET_DIR: tests/mcp_v2 reads ``<repo>/target/debug/tilth``, and the root .gitignore
    ``/target`` matches a symlink.
    """
    target = Path(worktree) / "target"
    if not target.is_symlink() and not target.exists():
        shared = run.candidate_target_dir()
        shared.mkdir(parents=True, exist_ok=True)
        target.symlink_to(shared, target_is_directory=True)
    # A new session lets a timeout kill cargo and the test binaries too, not only ``just``.
    process = subprocess.Popen(["just", "check"], cwd=worktree, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env={**run.build_tool_env(), PHRASE_PIN_EXEMPT_ENV: "1"},
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=JUST_CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return False, f"just check timed out after {JUST_CHECK_TIMEOUT_S} s"
    return process.returncode == 0, stdout + stderr


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--panel", type=Path, required=True, help="pre-registered panel file")
    parser.add_argument("--max-usd", type=float, help="required: one spend ceiling for every paid call in the run")
    parser.add_argument("--max-metric-calls", type=int, required=True,
                        help="GEPA budget: evaluator calls, one per (candidate, dev task)")
    parser.add_argument("--seed-sha", required=True, help="the tilth commit every candidate builds on")
    parser.add_argument("--base-branch", default="main",
                        help="the winner PR's base; --seed-sha must be on origin/<branch>, or on the local "
                             "branch when there is no remote-tracking ref (default: main)")
    # Only the claude runner enforces the tilth_forced tool restriction that candidates run under.
    claude_models = sorted(name for name in run.MODELS if run.RUNNERS[name] == "claude")
    parser.add_argument("--model", default="haiku", choices=claude_models, help="agent model for every cell")
    parser.add_argument("--reruns", type=int, default=3, help="re-runs R before a candidate is accepted (default 3)")
    parser.add_argument("--plateau", type=int, default=5,
                        help="stop after P paid-tier candidates leave the best dev mean unimproved (default 5)")
    parser.add_argument("--cell-budget-usd", type=float, default=run.DEFAULT_MAX_BUDGET_USD,
                        help="Claude --max-budget-usd for every cell; part of each cell's run key "
                             f"(default {run.DEFAULT_MAX_BUDGET_USD})")
    parser.add_argument("--cell-estimate-usd", type=float,
                        help="estimate for a call with no stored or earlier cost (default: --cell-budget-usd)")
    parser.add_argument("--refreeze-baselines", action="store_true",
                        help="buy drifted baseline cells again under their new key")
    parser.add_argument("--no-strict-file-tools", dest="strict_file_tools", action="store_false",
                        help="run cells without the Bash file-tool hook (strict is the default: candidates run "
                             "their own hook, the baseline runs the seed's)")
    parser.add_argument("--replay-diagnostic", type=int, default=0, metavar="N",
                        help="for a candidate that changes src_patch, replay N seed dev cells against the seed and "
                             "candidate binaries and show the diff to later reflection (no model call; never "
                             "rejects; default 0 = off)")
    parser.add_argument("--directives", type=Path, metavar="PATH",
                        help="JSON list of {id, component, brief, hypothesis?}: seeded proposals, tried in order "
                             "before the selector picks a component itself (default: none)")
    parser.add_argument("--no-repair", dest="repair", action="store_false",
                        help="do not re-call the proposer once when its src_patch child fails apply or just check "
                             "(repair is the default)")
    parser.add_argument("--deep-diagnosis", action="store_true",
                        help="before each proposal, run Codex once per parent over its dev trajectories and show "
                             "its ranked causes to the proposer (off by default; Codex spend is not on the ledger)")
    parser.add_argument("--deep-diagnosis-model", default=deepdiag.DEFAULT_MODEL,
                        help=f"Codex model (default {deepdiag.DEFAULT_MODEL})")
    parser.add_argument("--deep-diagnosis-effort", default=deepdiag.DEFAULT_EFFORT,
                        help=f"Codex reasoning effort (default {deepdiag.DEFAULT_EFFORT})")
    parser.add_argument("--deep-diagnosis-max", type=int, default=deepdiag.DEFAULT_MAX_RUNS,
                        help=f"Codex runs per evolve run (default {deepdiag.DEFAULT_MAX_RUNS})")
    parser.add_argument("--deep-diagnosis-timeout", type=float, default=deepdiag.DEFAULT_TIMEOUT_S,
                        help=f"seconds before a Codex run is killed (default {deepdiag.DEFAULT_TIMEOUT_S})")
    parser.add_argument("--allow-cli-drift", action="store_true",
                        help="run with a Claude CLI that is not the version in benchmark/claude-cli.version; "
                             "every stored cell's run key then drifts and is bought again")
    parser.add_argument("--budget-id", help="share one persistent spend file across runs under this name")
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _base_ref(repo: Path, branch: str) -> str:
    """The fully qualified remote ref the winner PR merges into, or the local branch ref when absent.

    A local branch can lag or lead the remote one; the draft PR targets the remote branch.
    """
    remote = f"refs/remotes/origin/{branch}"
    found = subprocess.run(["git", "rev-parse", "--verify", "--quiet", remote], cwd=repo,
                           capture_output=True)
    return remote if found.returncode == 0 else f"refs/heads/{branch}"


def build(argv: list[str] | None = None, *,
          judge_factory: Callable[[SpendLedger, float], judge.Judge] | None = None,
          pr_client: PRClient | None = None, just_check: Callable[[Path], tuple[bool, str]] | None = None,
          spawn: Callable | None = None, auth_spawn: Callable | None = None,
          deep_runner: deepdiag.Runner | None = None) -> Evolution | int:
    """Validate the flags and build the run, or return an exit code; no model call happens here."""
    args = _parser().parse_args(argv)
    if args.max_usd is None:
        return _error("--max-usd is required: it bounds every cell, judge, reflection, and proposer call")
    if args.cell_estimate_usd is None:
        args.cell_estimate_usd = args.cell_budget_usd
    for flag, value in (("--max-usd", args.max_usd), ("--cell-budget-usd", args.cell_budget_usd),
                        ("--cell-estimate-usd", args.cell_estimate_usd)):
        if not math.isfinite(value) or value <= 0:
            return _error(f"{flag} must be a positive finite number")
    if args.max_metric_calls < 1 or args.reruns < 0 or args.plateau < 1 or args.replay_diagnostic < 0:
        return _error("--max-metric-calls and --plateau must be at least 1, --reruns and --replay-diagnostic "
                      "at least 0")
    try:
        auth.refuse_env(os.environ)
    except auth.ClaudeAuthError as error:
        return _error(str(error))
    try:
        print(run.require_pinned_cli(allow_drift=args.allow_cli_drift))
    except run.CliPinError as error:
        return _error(str(error))
    if args.deep_diagnosis_max < 0 or not math.isfinite(args.deep_diagnosis_timeout) \
            or args.deep_diagnosis_timeout <= 0:
        return _error("--deep-diagnosis-max must be at least 0 and --deep-diagnosis-timeout positive")
    repo = args.repo.resolve()
    try:
        seed_sha = git("rev-parse", "--verify", f"{args.seed_sha}^{{commit}}", cwd=repo).strip()
        seed = read_seed(repo, seed_sha)
    except (GitError, SeedError) as error:
        return _error(str(error))
    base_ref = _base_ref(repo, args.base_branch)
    if subprocess.run(["git", "merge-base", "--is-ancestor", seed_sha, base_ref], cwd=repo,
                      capture_output=True).returncode != 0:
        base_name = base_ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")
        return _error(f"--seed-sha {args.seed_sha} is not an ancestor of {base_name} (--base-branch {args.base_branch})")
    try:
        panel = panels.load_panel(args.panel, store_path=run.RESULTS_DIR / baselines.STORE_FILENAME)
        panel.register(run.TASKS)
    except panels.PanelError as error:
        return _error(str(error))
    queue: list[directives.Directive] = []
    if args.directives is not None:
        try:
            queue = directives.load(args.directives, seed, forbidden_terms(args.panel))
        except directives.DirectiveError as error:
            return _error(str(error))
    cells = [(task, "baseline", args.model) for task in [*panel.cheap, *panel.dev, *panel.test]]
    try:
        auth.preflight_claude_auth([run.isolated_call_target(), *run.claude_auth_targets(cells, bare=True)],
                                   spawn=auth_spawn)
        ledger = (SpendLedger.persistent(args.budget_id, args.max_usd) if args.budget_id is not None
                  else SpendLedger(args.max_usd))
    except (auth.ClaudeAuthError, ValueError) as error:
        return _error(str(error))
    factory = judge_factory or (lambda shared, floor: judge.Judge(shared, cell_estimate_usd=floor))
    settings = Settings(
        run_id=args.run_id, repo=repo, seed_sha=seed_sha, base_branch=args.base_branch, model=args.model,
        reruns=args.reruns, plateau=args.plateau, max_usd=args.max_usd, max_metric_calls=args.max_metric_calls,
        cell_estimate_usd=args.cell_estimate_usd, cell_budget_usd=args.cell_budget_usd,
        refreeze_baselines=args.refreeze_baselines, panel_path=args.panel,
        strict_file_tools=args.strict_file_tools, replay_diagnostic=args.replay_diagnostic,
        directives=tuple(queue), repair=args.repair, deep_diagnosis=args.deep_diagnosis,
        deep_diagnosis_model=args.deep_diagnosis_model, deep_diagnosis_effort=args.deep_diagnosis_effort,
        deep_diagnosis_max=args.deep_diagnosis_max, deep_diagnosis_timeout=args.deep_diagnosis_timeout,
    )
    return Evolution(settings, panel=panel, ledger=ledger, judge_=factory(ledger, args.cell_estimate_usd), seed=seed,
                     pr_client=pr_client or GitHubPRClient(repo), just_check=just_check or default_just_check,
                     spawn=spawn or subprocess.run, deep_runner=deep_runner)


def main(argv: list[str] | None = None, **deps) -> int:
    evolution = build(argv, **deps)
    if isinstance(evolution, int):
        return evolution
    try:
        return evolution.run()
    except (EvolveError, auth.ClaudeAuthError) as error:
        evolution.log(f"error: {error}")
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
