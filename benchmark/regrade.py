#!/usr/bin/env python3
"""Offline re-grade of stored benchmark answers under grader alternation.

The grader now supports "a|b" alternation in GroundTruth.required_strings (see
tasks/base.py). Past results stored each agent's answer in `result_text`
(truncated at 5000 chars), so historical false negatives can be recovered
WITHOUT re-running the benchmark: re-apply the now alternation-aware text grading
to the stored answer and see which previously-failed nav/grok tasks now pass.

Only pure text-graded tasks are re-gradeable offline. Mutation tasks (graded by
test_command) and forward-edit tasks (graded by git diff) need a live repo, so
they are skipped — their oracle is unchanged anyway.

Caveat: stored answers are truncated at 5000 chars, so a string the live grader
matched beyond that point is invisible here. This can only make re-grading
stricter, so it never invents a recovery; it could in theory show a spurious
regression (reported separately).

Caveat: re-grading compares stored answers against the CURRENT ground truths, so a
recovery (incorrect -> correct) is only attributable to alternation when the ground
truth changed *only* by gaining alternations since the run. An unrelated edit to a
required string between the run and now can masquerade as a recovery.

    python benchmark/regrade.py <results.jsonl>
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tasks import TASKS
from tasks.base import required_matches


def is_text_graded(task) -> bool:
    """True for tasks graded purely by substring matching on the answer (no repo
    needed): no mutations (test_command oracle) and not a diff-checked edit."""
    if task.mutations:
        return False
    if task.task_type == "edit" and task.ground_truth.file_path:
        return False
    return True


def regrade_text(task, result_text: str) -> bool:
    """Re-apply the alternation-aware text grading to a stored answer, mirroring
    check_correctness's text path with no diff available offline."""
    gt = task.ground_truth
    text_lower = result_text.replace("`", "").lower()
    for required in gt.required_strings:
        if not required_matches(required, text_lower):
            return False
    for forbidden in gt.forbidden_strings:
        if forbidden.lower() in text_lower:
            return False
    return True


def regrade_file(records: list[dict]) -> tuple[list[dict], list[dict], int, int]:
    """Return (recovered, regressed, regradeable, skipped).

    recovered: stored incorrect -> now correct. regressed: stored correct -> now
    incorrect (expected empty; alternation only loosens matching).
    """
    recovered, regressed = [], []
    regradeable = skipped = 0
    for rec in records:
        if "error" in rec or "result_text" not in rec:
            skipped += 1
            continue
        task = TASKS.get(rec.get("task"))
        if task is None or not is_text_graded(task):
            skipped += 1
            continue
        regradeable += 1
        new_correct = regrade_text(task, rec["result_text"])
        old_correct = bool(rec.get("correct"))
        if new_correct and not old_correct:
            recovered.append(rec)
        elif old_correct and not new_correct:
            regressed.append(rec)
    return recovered, regressed, regradeable, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline re-grade under grader alternation")
    parser.add_argument("results_file", type=Path, help="Path to JSONL results file from run.py")
    args = parser.parse_args()

    if not args.results_file.exists():
        print(f"ERROR: File not found: {args.results_file}", file=sys.stderr)
        sys.exit(1)

    records = [json.loads(line) for line in args.results_file.read_text().splitlines() if line.strip()]
    recovered, regressed, regradeable, skipped = regrade_file(records)

    print("=" * 72)
    print("OFFLINE RE-GRADE under grader alternation")
    print("=" * 72)
    print(f"records:                                  {len(records)}")
    print(f"re-gradeable (text nav/grok tasks):       {regradeable}")
    print(f"skipped (mutation/edit/error/no answer):  {skipped}")
    print(f"recovered (was incorrect, now passes):    {len(recovered)}")
    if recovered:
        by_task: dict[str, int] = defaultdict(int)
        for r in recovered:
            by_task[r["task"]] += 1
        for task, n in sorted(by_task.items()):
            print(f"  + {task}: {n}")
    if regressed:
        print(f"WARNING: {len(regressed)} record(s) regressed (was correct, now fails); "
              "alternation only loosens — likely the 5000-char truncation caveat:")
        for r in regressed[:10]:
            print(f"  - {r['task']} rep{r.get('repetition')}")
    if regradeable and not recovered:
        print('\n(no recoveries: add "a|b" alternations to nav/grok ground truths to recover false negatives)')


if __name__ == "__main__":
    main()
