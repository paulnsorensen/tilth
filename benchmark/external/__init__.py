"""External benchmark tasks: FeatureBench Lite Level 1 and SWE-bench Multilingual, run natively.

Dataset rows, upstream mirrors, and admission verdicts live in the harness data
directory (``$TILTH_BENCH_DATA``), never in this checkout or an agent workdir.
Only ``fetch`` (via the preflight CLI) downloads.
"""

from . import contamination, download, featurebench, preflight, registry, swebench_ml
from .data import FEATUREBENCH_REVISION, SWEBENCH_ML_REVISION, cached_row, language_of
from .task import ExternalTask

__all__ = [
    "FEATUREBENCH_REVISION", "SWEBENCH_ML_REVISION", "ExternalTask", "cached_row", "contamination", "fetch",
    "featurebench", "language_of", "preflight", "resolve_task", "swebench_ml",
]


def fetch(dataset: str, **kwargs) -> None:
    """Download ``dataset``'s rows at the pinned revision and mirror its candidates."""
    download.fetch(dataset, **kwargs)


def resolve_task(name: str) -> ExternalTask | None:
    """The admitted ExternalTask for a cached instance ID, else None; never downloads."""
    if cached_row(name) is None:
        return None
    if not preflight.admit(name).admitted:
        return None
    return registry.load(name)
