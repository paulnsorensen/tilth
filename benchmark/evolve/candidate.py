"""Candidate components and content identity.

A candidate is five components: the four instruction texts that ``src/`` embeds
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
    """The seed candidate: the four instruction files' bytes at ``seed_sha`` and an empty ``src_patch``."""
    texts, missing = {}, []
    for name in TEXT_COMPONENTS:
        try:
            texts[name] = git_bytes("show", f"{seed_sha}:{name}", cwd=repo).decode("utf-8")
        except GitError:
            missing.append(name)
    if missing:
        raise SeedError(f"seed {seed_sha} is missing {', '.join(missing)}")
    return {**texts, SRC_PATCH: ""}
