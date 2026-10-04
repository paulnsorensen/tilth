"""Load the ExternalTask for any instance cached in either pinned dataset."""

from . import data, featurebench, swebench_ml
from .task import ExternalTask

_LOADERS = {data.FEATUREBENCH: featurebench.load, data.SWEBENCH_ML: swebench_ml.load}


def load(instance_id: str) -> ExternalTask:
    """The task for a cached instance ID; raises LookupError when neither dataset caches it."""
    dataset = data.cached_dataset(instance_id)
    if dataset is None:
        raise LookupError(f"{instance_id} has no cached row; run benchmark/external/preflight.py")
    return _LOADERS[dataset](instance_id, data.REVISIONS[dataset])


def candidates(dataset: str) -> list[str]:
    """The instances admission tries for a dataset: curated FeatureBench rows or the cherry picks."""
    return featurebench.candidates() if dataset == data.FEATUREBENCH else swebench_ml.candidates()
