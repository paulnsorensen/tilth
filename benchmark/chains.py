"""Chained cells: several tasks run back to back in ONE agent conversation.

A chain is a named, ordered list of tasks on the same local repo. The first task
runs as a normal ``claude -p`` call. Each next task resumes the same session in the
same workspace, so the context grows the way a real main thread grows.

How the setups compose: the workspace is prepared once per member, in order, on the
state the previous member left. This is sound only when the members do not
interfere, so ``parse_chain`` enforces these rules:

- every member has the same repo and the same ``hide_git`` value;
- mutation members touch disjoint files;
- at most one member grades the whole workspace (it overrides ``check_correctness``),
  and no member carries mutations next to it.

The committed chains put read, navigate, and grok tasks first and one heavy edit
task last, so an edit never changes a ground truth that a later member needs.

This module does not import ``run``: ``run`` injects the task digest function.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, MutableMapping, Sequence

from tasks.base import GroundTruth, Task

PREFIX = "chain:"
# Chain keys do not hash this file (run._task_source_files skips ChainTask), so a
# change to how a chain is driven or graded (chain_row, run._run_chain) must bump this.
CHAIN_PROTOCOL = 1

_SUM_FIELDS = (
    "total_cost_usd", "duration_ms", "wall_duration_ms", "num_turns", "num_tool_calls",
    "context_tokens", "output_tokens", "input_tokens", "cache_creation_tokens",
    "cache_creation_5m_tokens", "cache_creation_1h_tokens", "cache_read_tokens",
)
_LIST_FIELDS = ("per_turn_context_tokens", "per_turn_output_tokens", "per_turn_token_usage", "tool_sequence")
_MERGE_FIELDS = ("tool_calls", "batch_sizes", "op_kinds", "model_usage")


class ChainError(ValueError):
    """A chain definition or chain id is invalid."""


@dataclass(frozen=True)
class Chain:
    id: str
    members: tuple[str, ...]
    note: str = ""


CHAINS: dict[str, Chain] = {
    chain.id: chain
    for chain in (
        Chain(
            "gin-edit-5",
            ("gin_client_ip", "gin_middleware_chain", "gin_servehttp_flow", "grok_gin_new", "gin_edit_render_context"),
            "Four gin read tasks, then the render-context edit. gin_context_next and grok_context_next are left out: "
            "in a chain the agent answers them without the required HandlersChain string. Estimated 30-40 API calls.",
        ),
        Chain(
            "fastapi-edit-5",
            (
                "fastapi_dependency_resolution", "fastapi_request_validation", "fastapi_depends_internals",
                "grok_depends", "fastapi_edit_security_raise_on_missing",
            ),
            "Four fastapi read tasks, then the security migration edit. Estimated 40-50 API calls.",
        ),
        Chain(
            "rg-edit-7",
            (
                "rg_trait_implementors", "rg_flag_definition", "rg_search_dispatch", "rg_walker_parallel",
                "rg_lineiter_usage", "grok_rg_matcher", "rg_edit_flow_migration",
            ),
            "Six ripgrep read tasks, then the flow migration edit. Estimated 50-60 API calls.",
        ),
        Chain(
            "express-edit-6",
            (
                "express_json_send", "express_render_chain", "express_app_init", "express_res_send",
                "express_app_render", "express_edit_example_start",
            ),
            "Five express read tasks, then the example-start migration edit. Estimated 45-55 API calls.",
        ),
    )
}


def _grades_workspace(task: Task) -> bool:
    return type(task).check_correctness is not Task.check_correctness


class ChainTask(Task):
    """A registered chain. It carries its member tasks; ``run`` drives the turns."""

    def __init__(self, chain_id: str, members: Sequence[Task], digest: Callable[[Task], str]) -> None:
        self.chain_id = chain_id
        self.members = tuple(members)
        self._digest = digest

    @property
    def name(self) -> str:
        return PREFIX + self.chain_id

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def prompt(self) -> str:
        return "\n\n".join(member.prompt for member in self.members)

    @property
    def ground_truth(self) -> GroundTruth:
        return GroundTruth(required_strings=[], forbidden_strings=[])

    @property
    def repo(self) -> str:
        return self.members[0].repo

    @property
    def capability(self) -> str:
        return "chain"

    @property
    def hide_git(self) -> bool:
        return self.members[0].hide_git

    @property
    def fixture_path(self):
        return self.members[0].fixture_path

    @property
    def timeout_s(self) -> int | None:
        limits = [member.timeout_s for member in self.members if member.timeout_s is not None]
        return sum(limits) if limits else None

    def identity_inputs(self) -> dict[str, object]:
        return {
            "chain_protocol": CHAIN_PROTOCOL,
            "members": [[member.name, self._digest(member)] for member in self.members],
        }

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return False, "a chain is graded per sub-task"


def parse_chain(chain_id: str, member_names: Sequence[str], tasks: dict[str, Task]) -> list[Task]:
    """Resolve and validate a chain's members; raise ``ChainError`` when they cannot compose."""
    if len(member_names) < 2:
        raise ChainError(f"chain {chain_id}: needs at least 2 tasks")
    if len(set(member_names)) != len(member_names):
        raise ChainError(f"chain {chain_id}: duplicate task in {list(member_names)}")
    unknown = [name for name in member_names if name not in tasks]
    if unknown:
        raise ChainError(f"chain {chain_id}: unknown task {unknown[0]}")
    members = [tasks[name] for name in member_names]
    repos = {member.repo for member in members}
    if len(repos) != 1:
        raise ChainError(f"chain {chain_id}: tasks span several repos {sorted(repos)}")
    if len({member.hide_git for member in members}) != 1:
        raise ChainError(f"chain {chain_id}: tasks disagree on hide_git")
    graders = [member.name for member in members if _grades_workspace(member)]
    if len(graders) > 1:
        raise ChainError(f"chain {chain_id}: several tasks grade the whole workspace {graders}")
    if graders and any(member.mutations for member in members):
        raise ChainError(f"chain {chain_id}: {graders[0]} grades the workspace, so no task may carry mutations")
    seen: dict[str, str] = {}
    for member in members:
        for mutation in member.mutations:
            owner = seen.setdefault(mutation.file_path, member.name)
            if owner != member.name:
                raise ChainError(f"chain {chain_id}: {owner} and {member.name} both mutate {mutation.file_path}")
    return members


def register(tasks: MutableMapping[str, Task], name: str, *, digest: Callable[[Task], str]) -> None:
    """Register the ``chain:<id>`` task named ``name`` in ``tasks``; a known name is left alone."""
    if name in tasks:
        return
    chain_id = name[len(PREFIX):] if name.startswith(PREFIX) else name
    chain = CHAINS.get(chain_id)
    if chain is None:
        raise ChainError(f"unknown chain: {chain_id} (known: {', '.join(sorted(CHAINS))})")
    members = parse_chain(chain.id, chain.members, tasks)
    tasks[name] = ChainTask(chain.id, members, digest)


def _merge(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge(merged[key], value) if key in merged else value
        return merged
    if isinstance(left, list) and isinstance(right, list):
        return left + right
    numeric = (int, float)
    if isinstance(left, numeric) and isinstance(right, numeric) and not isinstance(left, bool) and not isinstance(right, bool):
        return left + right
    return right


def chain_row(task: ChainTask, rows: Sequence[dict], *, identity: dict, chain_budget: float) -> dict:
    """Fold one row per sub-task into the chain's row.

    ``correct`` is true only when every sub-task is correct, and ``pass_rate`` is the
    fraction that are. Totals and token lists cover the whole conversation.
    """
    row = dict(rows[-1])
    for field in _SUM_FIELDS:
        row[field] = sum(r.get(field) or 0 for r in rows)
    for field in _LIST_FIELDS:
        row[field] = [item for r in rows for item in r.get(field) or []]
    for field in _MERGE_FIELDS:
        merged: dict = {}
        for r in rows:
            merged = _merge(merged, r.get(field) or {})
        row[field] = merged
    sub_tasks = [
        {
            "task": r["task"],
            "correct": bool(r["correct"]),
            "reason": r["correctness_reason"],
            "cost_usd": r["total_cost_usd"],
            "api_calls": len(r["per_turn_context_tokens"]),
            "num_turns": r["num_turns"],
            "duration_ms": r["duration_ms"],
        }
        for r in rows
    ]
    passed = sum(1 for sub in sub_tasks if sub["correct"])
    trajectories = [r["trajectory_path"] for r in rows if r.get("trajectory_path")]
    row.update(
        {
            "sub_tasks": sub_tasks,
            "chain_id": task.chain_id,
            "chain_size": len(rows),
            "api_calls": len(row["per_turn_context_tokens"]),
            "correct": passed == len(sub_tasks),
            "pass_rate": passed / len(sub_tasks),
            "correctness_reason": f"{passed}/{len(sub_tasks)} sub-tasks correct",
            "contaminated": any(r.get("contaminated") is True for r in rows),
            "contamination_hits": [hit for r in rows for hit in r.get("contamination_hits") or []],
            "chain_max_budget_usd": chain_budget,
            "trajectory_paths": trajectories,
            "trajectory_path": trajectories[0] if trajectories else None,
            "session_id": rows[-1].get("session_id"),
            "capability": task.capability,
            "reference_changed_lines": None,
            **identity,
            "task": task.name,
            "reused": False,
        }
    )
    return row
