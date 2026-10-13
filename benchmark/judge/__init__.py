"""Pinned model judge for structural-tool applicability (labels) and rollout critiques.

The judge labels tasks ``strong``, ``weak``, or ``none``, critiques stripped rollouts
for reflection, and gates both on agreement with a hand-labelled calibration set.
It never changes a score: labels and critiques stay in ``config.JUDGE_DIR``.

This package exports nothing. Import the submodule you need: ``from judge import config, store``
loads no runner; ``from judge import core`` loads the runner.
"""
