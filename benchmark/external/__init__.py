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


def resolve_task(name: str) -> tuple[ExternalTask | None, preflight.PreflightVerdict | None]:
    """The admitted ExternalTask for a cached instance ID, with the one admission verdict; never downloads.

    An uncached name returns ``(None, None)``. A refused instance returns ``(None, verdict)``.
    """
    if cached_row(name) is None:
        return None, None
    verdict = preflight.admit(name)
    if not verdict.admitted:
        return None, verdict
    return registry.load(name), verdict
