"""Benchmark graduation gate: frozen-manifest schema + evaluator.

See `benchmark/graduation/schema.py` for the manifest format and
`benchmark/graduation/evaluate.py` for the CLI. Sample manifest at
`benchmark/graduation/manifest.example.json`.
"""

from .evaluate import evaluate
from .schema import BLOCKED, Manifest, Verdict, load_manifest

__all__ = ["BLOCKED", "Manifest", "Verdict", "load_manifest", "evaluate"]
