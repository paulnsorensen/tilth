"""Pinned model judge for structural-tool applicability (labels) and rollout critiques.

The judge labels tasks ``strong``, ``weak``, or ``none``, critiques stripped rollouts
for reflection, and gates both on agreement with a hand-labelled calibration set.
It never changes a score: labels and critiques stay in ``config.JUDGE_DIR``.
"""

import importlib

__all__ = [
    "Agreement", "CalibrationInvalid", "CalibrationSet", "ClaudeJudgeClient", "CritiqueRejected",
    "CritiqueWithheld", "Judge", "JudgeAnswerInvalid", "JudgeCallFailed", "JudgeQuota", "JudgeReply",
    "JudgeSpendCeiling", "Label", "UnresolvableTask", "default_resolve_task", "load_calibration",
    "stripped_record",
]
# Exports resolve on first use, so ``from judge import store`` (as in analyze.py) does
# not load the runner that ``core`` imports.
_EXPORTS = {**dict.fromkeys(__all__, ".core"), "Agreement": ".store"}


def __getattr__(name: str) -> object:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module, __name__), name)
