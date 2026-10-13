"""Synthetic external-benchmark instances for the external task tests.

Rows under ``fixtures/external`` use exactly the real dataset keys. Upstream
repositories are rebuilt from ``fixtures/external/projects.json`` with pinned
commit metadata, so each row's ``base_commit`` names the same commit on every host.
"""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "external"
OUTPUTS = FIXTURES / "outputs"

# Fixture upstreams stand in for the pinned candidate repository map.
REPO_LANGUAGES = {
    "fixture/calc": "python",
    "fixture/calcgo": "go",
    "fixture/calcrs": "rust",
    "fixture/shapes": "python",
}

SWE_PY = "fixture__calc-1"
SWE_GO = "fixture__calcgo-2"
SWE_RS = "fixture__calcrs-3"
FB_LV1 = "fixture__shapes.0a1b2c3d.test_area.5e6f7a8b.lv1"
FB_F2P_EDIT = "fixture__shapes.0a1b2c3d.test_area.9c0d1e2f.lv1"
FB_LV2 = "fixture__shapes.0a1b2c3d.test_area.5e6f7a8b.lv2"

_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


def git(*args: str, cwd: Path) -> str:
    env = {**os.environ, **_COMMIT_ENV, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout


def projects() -> dict[str, dict[str, str]]:
    return json.loads((FIXTURES / "projects.json").read_text())


def rows(dataset: str) -> dict[str, dict]:
    """Return the fixture rows of one dataset, keyed by instance ID."""
    return {row["instance_id"]: row for row in json.loads((FIXTURES / f"{dataset}_rows.json").read_text())}


def write_tree(root: Path, files: Mapping[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def build_upstream(repo: str, files: Mapping[str, str], root: Path) -> tuple[Path, str]:
    """Build a bare upstream at ``root/<repo>.git``: an initial commit, then the base commit."""
    work = root / "work" / repo
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=work)
    write_tree(work, {"README.md": f"{repo} fixture\n"})
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "initial", cwd=work)
    write_tree(work, files)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "base", cwd=work)
    base = git("rev-parse", "HEAD", cwd=work).strip()
    bare = root / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--bare", str(work), str(bare), cwd=root)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    return bare, base


def parquet_bytes(table_rows: list[dict]) -> bytes:
    """Encode rows the way the dataset host serves them."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(table_rows), sink)
    return sink.getvalue()


def run_python(code: str, *, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter with the benchmark directory importable."""
    benchmark = Path(__file__).parent.parent
    return subprocess.run([sys.executable, "-c", code], cwd=benchmark, env={**os.environ, **env},
                          capture_output=True, text=True, timeout=300)
