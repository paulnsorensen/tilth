"""Pinned judge settings and the paths of its prompts, calibration file, and caches."""

from pathlib import Path

from baselines import STORE_FILENAME
from config import RESULTS_DIR

# Full model ID of the current-generation sonnet (benchmark/config.py "sonnet5").
JUDGE_MODEL = "claude-sonnet-5"
# Unweighted Cohen's kappa each dimension must reach for the judge to count as calibrated.
KAPPA_THRESHOLD = 0.6
MIN_CALIBRATION_TRAJECTORIES = 20
# The fixed answer sets: an applicability label, and a critique's first-line verdict.
LABELS = ("strong", "weak", "none")
VERDICTS = ("apt", "missed", "misapplied")

PACKAGE_DIR = Path(__file__).parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CALIBRATION_FILE = PACKAGE_DIR / "calibration.json"
# Label and critique caches plus agreement records; gitignored.
JUDGE_DIR = RESULTS_DIR / "judge"
RESULT_STORE = RESULTS_DIR / STORE_FILENAME
