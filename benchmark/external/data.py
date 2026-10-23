"""The harness data directory: pinned dataset revisions, cached rows, and upstream mirrors.

Dataset rows, mirrors, and admission verdicts live only here, outside the tilth
checkout and every agent workdir. Nothing in this module downloads or writes.
"""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

FEATUREBENCH = "featurebench"
SWEBENCH_ML = "swebench_ml"
# Commit that the Hugging Face tag v1.1 pointed to on 2026-10-13.
FEATUREBENCH_REVISION = "76b4a4566e04f4bcc13c35125d4f301791efa736"
SWEBENCH_ML_REVISION = "846e647b9f33c0b51b739d005d13d85493c9af09"
REVISIONS = {FEATUREBENCH: FEATUREBENCH_REVISION, SWEBENCH_ML: SWEBENCH_ML_REVISION}
# Hugging Face dataset repository and parquet file per dataset.
DATASET_FILES = {
    FEATUREBENCH: ("LiberCoders/FeatureBench", "data/lite-00000-of-00001.parquet"),
    SWEBENCH_ML: ("SWE-bench/SWE-bench_Multilingual", "data/test-00000-of-00001.parquet"),
}
# sha256 of each pinned parquet file. A download that does not match is refused.
PARQUET_SHA256 = {
    FEATUREBENCH: "8740f33eb89e443e8e32b46c98508be05a2db2cbf65f856ff38e4e7730b1cc99",
    SWEBENCH_ML: "92abca7cb527b41a9f66d03a26ce441ff7319e3a49f985998fd56be4bb9b08b2",
}
UPSTREAM_URL = "https://github.com/{repo}.git"
DEFAULT_DATA_DIR = "~/.local/share/tilth-bench/external"

# The pinned candidate repository map: every FeatureBench Lite repository is Python.
REPO_LANGUAGES = {
    **{repo: "python" for repo in (
        "Lightning-AI/pytorch-lightning", "Netflix/metaflow", "astropy/astropy", "huggingface/transformers",
        "huggingface/trl", "linkedin/Liger-Kernel", "mlflow/mlflow", "mwaskom/seaborn", "pandas-dev/pandas",
        "pydantic/pydantic", "pydata/xarray", "sphinx-doc/sphinx", "sympy/sympy",
    )},
    "gin-gonic/gin": "go",
    "prometheus/prometheus": "go",
    "hashicorp/terraform": "go",
    "caddyserver/caddy": "go",
    "sharkdp/bat": "rust",
    "tokio-rs/tokio": "rust",
    "tokio-rs/axum": "rust",
    "astral-sh/ruff": "rust",
    "burntsushi/ripgrep": "rust",
    "uutils/coreutils": "rust",
    # JavaScript covers TypeScript too: the test runners transpile it.
    **{repo: "javascript" for repo in (
        "axios/axios", "mrdoob/three.js", "vuejs/core", "facebook/docusaurus", "preactjs/preact",
    )},
}
# npm names each JavaScript repo publishes. A monorepo's root package.json names a private workspace, not these.
# A trailing "/*" covers every package in the scope.
NPM_PACKAGES = {
    "axios/axios": ("axios",),
    "mrdoob/three.js": ("three",),
    "vuejs/core": ("vue", "@vue/*"),
    "facebook/docusaurus": ("@docusaurus/*", "create-docusaurus"),
    "preactjs/preact": ("preact",),
}
LANGUAGES = ("python", "go", "rust", "javascript")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def data_dir() -> Path:
    """``$TILTH_BENCH_DATA``, or ``~/.local/share/tilth-bench/external``."""
    return Path(os.environ.get("TILTH_BENCH_DATA") or DEFAULT_DATA_DIR).expanduser().resolve()


def is_safe_id(instance_id: str) -> bool:
    return bool(_SAFE_NAME.match(instance_id)) and ".." not in instance_id


def rows_dir(dataset: str, revision: str) -> Path:
    return data_dir() / "rows" / dataset / revision


def row_path(dataset: str, revision: str, instance_id: str) -> Path:
    if not is_safe_id(instance_id):
        raise ValueError(f"unsafe instance id: {instance_id!r}")
    return rows_dir(dataset, revision) / f"{instance_id}.json"


def mirror_path(repo: str) -> Path:
    """The bare upstream mirror for an ``owner/name`` repository."""
    return data_dir() / "mirrors" / f"{repo.replace('/', '__')}.git"


def upstream_url(repo: str) -> str:
    return UPSTREAM_URL.format(repo=repo)


def cached_dataset(instance_id: str) -> str | None:
    """The pinned dataset whose cached rows hold ``instance_id``, or None."""
    if not is_safe_id(instance_id):
        return None
    for dataset, revision in REVISIONS.items():
        if row_path(dataset, revision, instance_id).is_file():
            return dataset
    return None


def cached_row(instance_id: str) -> dict | None:
    """The full cached row for ``instance_id`` at its dataset's pinned revision, or None."""
    dataset = cached_dataset(instance_id)
    if dataset is None:
        return None
    return json.loads(row_path(dataset, REVISIONS[dataset], instance_id).read_text())


def language_of(row: Mapping) -> str:
    """The row's ``language`` column, else the language its ``repo`` maps to."""
    if row.get("language"):
        return row["language"]
    language = REPO_LANGUAGES.get(row.get("repo", ""))
    if language is None:
        raise ValueError(f"repository {row.get('repo')!r} is not in the pinned candidate map")
    return language
