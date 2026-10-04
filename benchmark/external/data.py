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
FEATUREBENCH_REVISION = "v1.1"
SWEBENCH_ML_REVISION = "846e647b9f33c0b51b739d005d13d85493c9af09"
REVISIONS = {FEATUREBENCH: FEATUREBENCH_REVISION, SWEBENCH_ML: SWEBENCH_ML_REVISION}
# Hugging Face dataset repository and parquet file per dataset.
DATASET_FILES = {
    FEATUREBENCH: ("LiberCoders/FeatureBench", "data/lite-00000-of-00001.parquet"),
    SWEBENCH_ML: ("SWE-bench/SWE-bench_Multilingual", "data/test-00000-of-00001.parquet"),
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
    "sharkdp/bat": "rust",
    "tokio-rs/tokio": "rust",
}
LANGUAGES = ("python", "go", "rust")

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
