"""``fetch``: the only code path that downloads dataset rows or upstream source."""

import io
import json
import os
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

from . import data, proc, registry

_COMPLETE_MARKER = "_complete"
_HTTP_TIMEOUT_S = 300


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_S) as response:
        return response.read()


def _parquet_rows(payload: bytes) -> list[dict]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise RuntimeError("fetching dataset rows needs pyarrow: pip install pyarrow") from error
    return parquet.read_table(io.BytesIO(payload)).to_pylist()


def write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so an interrupted write never leaves a truncated file."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def dataset_url(dataset: str) -> str:
    """The parquet file of ``dataset`` at its pinned revision."""
    repository, path = data.DATASET_FILES[dataset]
    return f"https://huggingface.co/datasets/{repository}/resolve/{data.REVISIONS[dataset]}/{path}"


def _has_commit(mirror, commit: str) -> bool:
    return proc.run(["git", f"--git-dir={mirror}", "cat-file", "-e", f"{commit}^{{commit}}"]).returncode == 0


def ensure_mirror(repo: str, commit: str) -> None:
    """Make the bare mirror of ``repo`` hold ``commit``, fetching it only when absent."""
    mirror = data.mirror_path(repo)
    if not mirror.is_dir():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        init = proc.run(["git", "init", "-q", "--bare", str(mirror)])
        if init.returncode:
            raise RuntimeError(f"cannot create mirror {mirror}: {init.stderr.strip()}")
    if _has_commit(mirror, commit):
        return
    fetched = proc.run(["git", f"--git-dir={mirror}", "fetch", "-q", "--depth", "1",
                        data.upstream_url(repo), commit], timeout=3600)
    if fetched.returncode or not _has_commit(mirror, commit):
        raise RuntimeError(f"cannot fetch {commit} from {data.upstream_url(repo)}: {fetched.stderr.strip()[-500:]}")


def fetch(dataset: str, *, client: Callable[[str], bytes] | None = None, extra_ids: Iterable[str] = ()) -> None:
    """Cache ``dataset``'s rows at the pinned revision and mirror each candidate's ``base_commit``.

    Rows already cached under the revision and mirrors that already hold the
    commit are not fetched again. ``extra_ids`` names more instances to mirror,
    such as panel members outside the candidate set.
    """
    revision = data.REVISIONS[dataset]
    target = data.rows_dir(dataset, revision)
    if not (target / _COMPLETE_MARKER).is_file():
        url = dataset_url(dataset)
        rows = _parquet_rows((client or _http_get)(url))
        target.mkdir(parents=True, exist_ok=True)
        for row in rows:
            if data.is_safe_id(str(row.get("instance_id", ""))):
                write_atomically(data.row_path(dataset, revision, row["instance_id"]), json.dumps(row, default=str))
        (target / _COMPLETE_MARKER).write_text(url + "\n")
    for instance_id in dict.fromkeys([*registry.candidates(dataset), *extra_ids]):
        if data.cached_dataset(instance_id) == dataset:
            row = data.cached_row(instance_id)
            ensure_mirror(row["repo"], row["base_commit"])
