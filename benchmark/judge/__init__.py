"""Pinned model judge for structural-tool applicability (labels) and rollout critiques.

The judge labels tasks ``strong``, ``weak``, or ``none``, critiques stripped rollouts
for reflection, and gates both on agreement with a hand-labelled calibration set.
It never changes a score: labels and critiques stay in ``config.JUDGE_DIR``.
"""

from .core import (
    CalibrationInvalid,
    CalibrationSet,
    ClaudeJudgeClient,
    CritiqueRejected,
    CritiqueWithheld,
    Judge,
    JudgeAnswerInvalid,
    JudgeCallFailed,
    JudgeQuota,
    JudgeReply,
    JudgeSpendCeiling,
    Label,
    UnresolvableTask,
    default_resolve_task,
    load_calibration,
    stripped_record,
)
from .store import Agreement

__all__ = [
    "Agreement", "CalibrationInvalid", "CalibrationSet", "ClaudeJudgeClient", "CritiqueRejected",
    "CritiqueWithheld", "Judge", "JudgeAnswerInvalid", "JudgeCallFailed", "JudgeQuota", "JudgeReply",
    "JudgeSpendCeiling", "Label", "UnresolvableTask", "default_resolve_task", "load_calibration",
    "stripped_record",
]
