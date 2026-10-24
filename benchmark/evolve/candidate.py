"""Candidate components and content identity.

A candidate is five components: the four prompt texts that ``src/`` embeds
with ``include_str!`` and ``src_patch``, the cumulative unified diff of the
candidate's ``src/**`` against the seed commit. Its identity is a hash of its
content, never a GEPA pool index.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from .gitops import GitError, git_bytes

TEXT_COMPONENTS = ("prompts/mcp.md", "prompts/tools/read.md", "prompts/tools/search.md", "prompts/tools/write.md")
SRC_PATCH = "src_patch"
COMPONENTS = (*TEXT_COMPONENTS, SRC_PATCH)
# The MCP host truncates the instructions field and each tool description past 2048 bytes; the src/mcp tests
# (server_instructions_fit_character_cap, tool_descriptions_fit_2kb) reject a longer prompt file.
MAX_BYTES = {name: 2048 for name in TEXT_COMPONENTS}
# What reflection and the src proposer optimize: the evolve score (``loop.task_score``), stated in both prompts.
OBJECTIVE = (
    "Each rollout uses only tilth's tools and is scored against the same task run with the host's built-in "
    "tools. A task's score is its pass rate times the built-in run's mean cost divided by this candidate's "
    "mean cost per rollout, so 1.0 is parity and a cheaper rollout at the same pass rate scores higher. "
    "A `scores` record gives each task's pass_rate, mean_cost, and baseline_cost (USD). A `cost_pairs` record "
    "pairs the candidate's and the built-in run's turns, tool calls, and input, output, and cache tokens for one "
    "task. Cost grows with the context carried into each API call and with the number of calls."
)


class SeedError(ValueError):
    """The seed commit lacks an instruction file a candidate needs."""


def content_id(candidate: Mapping[str, str]) -> str:
    """sha256 of the canonical JSON (sorted keys, UTF-8, no insignificant whitespace) of the components."""
    canonical = json.dumps(dict(candidate), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def short_id(candidate: Mapping[str, str]) -> str:
    return content_id(candidate)[:12]


def ref_name(run_id: str, candidate: Mapping[str, str]) -> str:
    """The local ref ``evolve/<run-id>/<content-id>`` (stored under ``refs/``, outside the branch namespace)."""
    return f"refs/evolve/{run_id}/{short_id(candidate)}"


def read_seed(repo: Path, seed_sha: str) -> dict[str, str]:
    """The seed candidate: the four prompt files' bytes at ``seed_sha`` and an empty ``src_patch``."""
    texts, missing = {}, []
    for name in TEXT_COMPONENTS:
        try:
            texts[name] = git_bytes("show", f"{seed_sha}:{name}", cwd=repo).decode("utf-8")
        except GitError:
            missing.append(name)
    if missing:
        raise SeedError(f"seed {seed_sha} is missing {', '.join(missing)}")
    return {**texts, SRC_PATCH: ""}
