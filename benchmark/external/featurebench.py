"""FeatureBench Lite (v1.1) Level 1 instances.

A row's ``patch`` is the mask: it applies forward to ``base_commit`` and removes
the feature. Prepare applies only its hunks for files outside FAIL_TO_PASS, then
``test_patch``, which deletes the FAIL_TO_PASS test files. The gold patch is the
reverse of those mask hunks. Grading restores every FAIL_TO_PASS and
PASS_TO_PASS file from ``base_commit`` and aggregates pytest results per file.
"""

import json
import re
import shlex
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path

from . import data, patches, proc
from .task import ExternalTask, PrepareError, apply_patch, read_row, write_or_remove

# COSPA featurebench_lite_pareto12_v1.json task_ids (accessed 2026-10-03).
PARETO12_IDS = (
    "Lightning-AI__pytorch-lightning.126fa6f1.test_data.c8b292af.lv1",
    "sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1",
    "Netflix__metaflow.b390a8d4.test_stub_generator.7bf08c98.lv1",
    "astropy__astropy.b0db0daa.test_table.48eef659.lv1",
    "huggingface__transformers.e2e8dbed.test_serve.4e7860c7.lv1",
    "mlflow__mlflow.93dab383.test_databricks_tracing_utils.8ef44eb4.lv1",
    "mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1",
    "mwaskom__seaborn.7001ebe7.test_algorithms.1f0181c2.lv1",
    "pandas-dev__pandas.82fa2715.test_concat.ebe5de39.lv1",
    "pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1",
    "pydata__xarray.97f3a746.test_backends_chunks.fa55f68a.lv1",
    "sympy__sympy.c1097516.test_nullspace.f14fc970.lv1",
)
# Heavy-environment repositories the parent defers (bench-heavy-python-envs).
DEFERRED_REPOS = frozenset({
    "pandas-dev/pandas", "astropy/astropy", "huggingface/transformers", "Lightning-AI/pytorch-lightning",
    "mlflow/mlflow",
})
_PASSING = {"PASSED", "XFAIL", "XPASS"}
_RESULT_LINE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+?)(?: - .*)?$")


def level_of(instance_id: str) -> str | None:
    """``lv1`` or ``lv2`` from the instance ID suffix, else None."""
    suffix = instance_id.rsplit(".", 1)[-1]
    return suffix if suffix in {"lv1", "lv2"} and "." in instance_id else None


def repo_of(instance_id: str) -> str:
    """``owner/name`` from an ``owner__name.<commit>...`` instance ID."""
    return instance_id.split(".", 1)[0].replace("__", "/", 1)


def candidates(pareto_ids: Sequence[str] | None = None) -> list[str]:
    """The Level 1 Pareto-12 instances outside the deferred heavy-environment repositories."""
    return [instance_id for instance_id in (PARETO12_IDS if pareto_ids is None else pareto_ids)
            if level_of(instance_id) == "lv1" and repo_of(instance_id) not in DEFERRED_REPOS]


def pytest_results(output: str) -> list[tuple[str, str]]:
    """``(status, node ID)`` for every result line of ``pytest -rA`` output."""
    return [(match[1], match[2]) for line in output.splitlines() if (match := _RESULT_LINE.match(line))]


def file_verdicts(output: str, entries: Sequence[str]) -> dict[str, bool]:
    """A file entry passes when pytest collected at least one ``<path>::`` node and all of them passed."""
    results = pytest_results(output)
    verdicts = {}
    for entry in entries:
        statuses = [status for status, node in results if node.startswith(f"{entry}::")]
        verdicts[entry] = bool(statuses) and all(status in _PASSING for status in statuses)
    return verdicts


class FeatureBenchTask(ExternalTask):
    dataset = data.FEATUREBENCH
    transformation = "mask_patch_forward"

    @cached_property
    def settings(self) -> dict:
        settings = self.row.get("repo_settings") or {}
        return json.loads(settings) if isinstance(settings, str) else dict(settings)

    @cached_property
    def mask_patch(self) -> str:
        """The row ``patch`` hunks for files outside FAIL_TO_PASS."""
        held_out = set(self.fail_to_pass)
        return "".join(section for path, section in patches.split_files(self.row["patch"]) if path not in held_out)

    @cached_property
    def gold_patch(self) -> str:
        return patches.reverse(self.mask_patch)

    @property
    def python_version(self) -> str | None:
        image = str(self.settings.get("base_image") or "")
        match = re.fullmatch(r"python(\d)(\d+)", image)
        return f"{match[1]}.{match[2]}" if match else None

    def declared_package(self) -> str | None:
        return self.settings.get("library_name") or None

    def apply_mask(self, tree: Path) -> None:
        if self.mask_patch and not apply_patch(tree, self.mask_patch):
            raise PrepareError("the row patch hunks outside FAIL_TO_PASS do not apply to base_commit")
        if not apply_patch(tree, self.row["test_patch"]):
            raise PrepareError("test_patch does not apply after the mask")

    def install_steps(self) -> list[str]:
        steps = [str(step) for step in self.settings.get("pre_install") or []]
        steps += [step.strip() for step in str(self.settings.get("install") or "").split("&&") if step.strip()]
        packages = [str(package) for package in self.settings.get("pip_packages") or []]
        if packages:
            steps.append(shlex.join(["pip", "install", *packages]))
        return steps

    def restore_heldout(self, checkout: Path) -> None:
        for relative in (*self.fail_to_pass, *self.pass_to_pass):
            write_or_remove(checkout / relative, self.base_file(relative))

    def _pytest_options(self) -> list[str]:
        tokens = shlex.split(str(self.settings.get("test_cmd") or "pytest -rA"))
        for prefix in (["python", "-m", "pytest"], ["python3", "-m", "pytest"], ["pytest"]):
            if tokens[:len(prefix)] == prefix:
                tokens = tokens[len(prefix):]
                break
        return tokens if "-rA" in tokens else ["-rA", *tokens]

    def test_output(self, checkout: Path, workdir: Path) -> str:
        argv = [str(workdir / ".venv" / "bin" / "python"), "-m", "pytest", *self._pytest_options(),
                "--continue-on-collection-errors", "-p", "no:cacheprovider",
                *self.fail_to_pass, *self.pass_to_pass]
        result = proc.run(argv, cwd=checkout, env=self.venv_env(workdir), timeout=self.timeout_s)
        return result.stdout + result.stderr

    def entry_results(self, output: str) -> dict[str, bool]:
        return file_verdicts(output, [*self.fail_to_pass, *self.pass_to_pass])


def load(instance_id: str, data_rev: str) -> FeatureBenchTask:
    """The FeatureBench task for a row cached at the pinned revision; raises for any other revision."""
    return FeatureBenchTask(read_row(data.FEATUREBENCH, data_rev, instance_id), data_rev)
