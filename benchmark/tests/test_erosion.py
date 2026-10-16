"""Erosion-guard row fields: workspace_diff_lines, reference_changed_lines, lint_ok, lint_reason."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
import baselines
import erosion
import run
from config import ModeConfig
from tasks.base import GroundTruth, Task, TaskSource

STOCK_STREAM = (Path(__file__).parent / "fixtures" / "streams" / "claude_native_cost.jsonl").read_text()
FIELDS = ("workspace_diff_lines", "reference_changed_lines", "lint_ok", "lint_reason")


class _ErosionTask(Task):
    name = "erosion_task"
    prompt = "Fix it."
    capability = "fix"
    ground_truth = GroundTruth()
    source = TaskSource(origin="fixture", license="MIT", commit_or_tag="pin", transformation="test-only")
    ref_lines = None
    lint = ()
    hidden = False
    restore: dict = {}

    @property
    def reference_changed_lines(self):
        return self.ref_lines

    @property
    def lint_command(self):
        return list(self.lint)

    @property
    def hide_git(self):
        return self.hidden

    def check_correctness(self, result_text, repo_path):
        for rel, text in self.restore.items():  # grading restores held-out test files
            target = Path(repo_path) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return True, "ok"


@pytest.fixture
def cell(monkeypatch, tmp_path):
    """Run one cell whose fake agent calls ``act(cwd)`` inside its workspace."""
    for key in auth.REFUSED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    source = tmp_path / "source"
    source.mkdir()
    mode = ModeConfig(name="stock_like", tools=["Read"], mcp_config_path=None, description="stock arm")
    monkeypatch.setitem(run.MODELS, "haiku", "configured-claude-model")
    monkeypatch.setitem(run.RUNNERS, "haiku", "claude")
    monkeypatch.setitem(run.MODES, mode.name, mode)
    monkeypatch.setattr(run, "get_repo_path", lambda _: source)

    def go(act, **task_fields):
        task = _ErosionTask()
        for key, value in task_fields.items():
            setattr(task, key, value)
        monkeypatch.setitem(run.TASKS, "erosion_task", task)

        def fake_run(cmd, *args, **kwargs):
            act(Path(kwargs["cwd"]))
            return subprocess.CompletedProcess(cmd, 0, stdout=STOCK_STREAM, stderr="")

        monkeypatch.setattr(run.subprocess, "run", fake_run)
        return run.run_single("erosion_task", mode.name, "haiku", 0)

    go.source = source
    return go


def test_diff_counts_edit_addition_deletion_and_ignores_binary(cell) -> None:
    (cell.source / "edit.txt").write_text("a\nb\nc\n")
    (cell.source / "gone.txt").write_text("1\n2\n3\n")
    (cell.source / "blob.bin").write_bytes(b"\x00\x01\x02")

    def act(cwd: Path) -> None:
        (cwd / "edit.txt").write_text("a\nB\nc\n")  # 1 removed + 1 added
        (cwd / "new.txt").write_text("x\ny\n")  # 2 added
        (cwd / "gone.txt").unlink()  # 3 removed
        (cwd / "blob.bin").write_bytes(b"\x00\x09\x08\x07")  # binary: 0

    row = cell(act)

    assert row["workspace_diff_lines"] == 7


def test_a_snapshot_git_timeout_does_not_abort_or_mislabel_the_cell(cell, monkeypatch) -> None:
    (cell.source / "keep.txt").write_text("same\n")
    ran: list[bool] = []

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git"], 60)

    monkeypatch.setattr(erosion, "_git", timeout)

    row = cell(lambda cwd: ran.append(True))

    assert ran == [True]  # the agent still ran
    assert row["workspace_diff_lines"] is None
    assert not row.get("timed_out")
    assert not row.get("error")

def test_untouched_workspace_counts_zero(cell) -> None:
    (cell.source / "keep.txt").write_text("same\n")

    assert cell(lambda cwd: None)["workspace_diff_lines"] == 0


def test_grading_time_test_restore_is_not_counted(cell) -> None:
    (cell.source / "src.py").write_text("x = 1\n")

    def act(cwd: Path) -> None:
        (cwd / "src.py").write_text("x = 2\n")

    row = cell(act, restore={"tests/test_big.py": "a\n" * 50})

    assert row["workspace_diff_lines"] == 2


def test_mined_task_with_hidden_git_ignores_git_paths(cell) -> None:
    (cell.source / ".git").mkdir()
    (cell.source / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (cell.source / "app.py").write_text("print(1)\n")

    def act(cwd: Path) -> None:
        assert not (cwd / ".git").exists()
        (cwd / ".git_hidden").mkdir()
        (cwd / ".git_hidden" / "junk").write_text("1\n2\n3\n")
        (cwd / "app.py").write_text("print(2)\n")

    row = cell(act, hidden=True)

    assert row["workspace_diff_lines"] == 2


def test_reference_changed_lines_comes_from_the_task(cell) -> None:
    assert cell(lambda cwd: None, ref_lines=12)["reference_changed_lines"] == 12
    assert cell(lambda cwd: None)["reference_changed_lines"] is None


def test_row_lint_pass_fail_and_no_command(cell) -> None:
    ok = cell(lambda cwd: None, lint=(sys.executable, "-c", "pass"))
    bad = cell(lambda cwd: None, lint=(sys.executable, "-c", "raise SystemExit(1)"))
    none = cell(lambda cwd: None)

    assert (ok["lint_ok"], ok["lint_reason"]) == (True, None)
    assert (bad["lint_ok"], bad["lint_reason"]) == (False, "failed (exit 1)")
    assert (none["lint_ok"], none["lint_reason"]) == (None, None)


def test_lint_sees_the_agent_result_and_cannot_change_what_grading_sees(cell) -> None:
    (cell.source / "f.txt").write_text("old\n")
    script = (
        "import pathlib,sys; p=pathlib.Path('f.txt'); ok=p.read_text()=='new\\n'; "
        "p.write_text('linted\\n'); pathlib.Path('lint_debris').write_text('x\\n'); sys.exit(0 if ok else 1)"
    )
    seen = {}

    class Probe(_ErosionTask):
        def check_correctness(self, result_text, repo_path):
            seen["f"] = (Path(repo_path) / "f.txt").read_text()
            seen["debris"] = (Path(repo_path) / "lint_debris").exists()
            return True, "ok"

    def act(cwd: Path) -> None:
        (cwd / "f.txt").write_text("new\n")

    row = cell(act, lint=(sys.executable, "-c", script), check_correctness=Probe().check_correctness)

    assert row["lint_ok"] is True
    assert seen == {"f": "new\n", "debris": False}
    assert row["workspace_diff_lines"] == 2


def test_lint_timeout_is_none_with_a_distinct_reason(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("x\n")

    result = erosion.lint_status([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout_s=0.5)

    assert result == (None, "timeout")


def test_lint_missing_binary_is_none_unavailable(tmp_path) -> None:
    assert erosion.lint_status(["no-such-linter-xyz"], tmp_path) == (None, "unavailable")


def test_diff_is_none_when_git_cannot_diff(tmp_path, monkeypatch) -> None:
    before, after = tmp_path / "b", tmp_path / "a"
    before.mkdir()
    after.mkdir()
    (before / "f").write_text("1\n")
    (after / "f").write_text("2\n")
    monkeypatch.setattr(erosion, "_RUN", lambda *a, **k: subprocess.CompletedProcess(a, 129, "", "boom"))

    assert erosion.workspace_diff_lines(before, after) is None


def test_failure_rows_carry_the_fields_as_none() -> None:
    assert erosion.UNMEASURED_FIELDS == dict.fromkeys(FIELDS)


def test_erosion_fields_stay_out_of_the_run_key() -> None:
    assert not set(FIELDS) & set(baselines.CELL_KEY_FIELDS)
    assert not set(FIELDS) & set(baselines.DIGEST_FIELDS)
    base = {field: "x" for field in (*baselines.CELL_KEY_FIELDS, *baselines.DIGEST_FIELDS)}

    assert baselines.run_key(base) == baselines.run_key({**base, **dict.fromkeys(FIELDS, 5)})


def test_git_ignored_tool_output_is_not_counted(cell) -> None:
    (cell.source / ".gitignore").write_text(".mypy_cache/\n*.egg-info/\n")
    (cell.source / "edit.txt").write_text("a\n")

    def act(cwd: Path) -> None:
        (cwd / "edit.txt").write_text("b\n")  # 1 removed + 1 added
        (cwd / ".mypy_cache").mkdir()
        (cwd / ".mypy_cache" / "meta.json").write_text("{}\n" * 50)
        (cwd / "pkg.egg-info").mkdir()
        (cwd / "pkg.egg-info" / "PKG-INFO").write_text("x\n" * 20)

    assert cell(act)["workspace_diff_lines"] == 2


def test_snapshot_leaves_out_ignored_files_and_harness_paths(tmp_path) -> None:
    work = tmp_path / "w"
    (work / ".mypy_cache").mkdir(parents=True)
    (work / "__pycache__").mkdir()
    (work / ".gitignore").write_text(".mypy_cache/\n")
    (work / "src.py").write_text("x = 1\n")
    (work / ".mypy_cache" / "m.json").write_text("{}\n")
    (work / "__pycache__" / "m.pyc").write_text("c\n")

    with erosion.snapshot(work) as baseline:
        names = sorted(p.relative_to(baseline).as_posix() for p in baseline.rglob("*") if p.is_file())

    assert names == [".gitignore", "src.py"]


def test_total_comes_from_one_git_diff_over_the_filtered_trees(tmp_path, monkeypatch) -> None:
    before, after = tmp_path / "b", tmp_path / "a"
    for side in (before, after):
        side.mkdir()
        (side / ".gitignore").write_text("junk/\n")
    for name in ("one", "two", "three"):
        (before / name).write_text("1\n2\n")
        (after / name).write_text("1\nX\n")  # 1 removed + 1 added each
    (after / "junk").mkdir()
    (after / "junk" / "big").write_text("z\n" * 100)
    calls: list[list[str]] = []
    real = erosion._RUN
    monkeypatch.setattr(erosion, "_RUN", lambda cmd, *a, **k: calls.append(list(cmd)) or real(cmd, *a, **k))

    assert erosion.workspace_diff_lines(before, after) == 6
    assert sum(cmd[:2] == ["git", "diff"] for cmd in calls) == 1


def test_an_agent_cannot_hide_a_new_file_by_ignoring_it(cell) -> None:
    (cell.source / "keep.txt").write_text("k\n")

    def act(cwd: Path) -> None:
        (cwd / "secret.py").write_text("a\nb\nc\n")  # 3 added
        (cwd / ".gitignore").write_text("secret.py\n")  # 1 added

    assert cell(act)["workspace_diff_lines"] == 4


def test_an_agent_cannot_hide_a_new_nested_file_by_ignoring_it(cell) -> None:
    (cell.source / "pkg").mkdir()
    (cell.source / "pkg" / "a.py").write_text("x\n")

    def act(cwd: Path) -> None:
        (cwd / "pkg" / "new.py").write_text("1\n2\n")  # 2 added
        (cwd / "pkg" / ".gitignore").write_text("*.py\n!a.py\n")  # 2 added

    assert cell(act)["workspace_diff_lines"] == 4


def test_deleting_an_ignore_rule_does_not_count_the_tool_output_it_covered(cell) -> None:
    (cell.source / ".gitignore").write_text(".mypy_cache/\n")

    def act(cwd: Path) -> None:
        (cwd / ".gitignore").unlink()  # 1 removed
        (cwd / ".mypy_cache").mkdir()
        (cwd / ".mypy_cache" / "meta.json").write_text("{}\n" * 50)

    assert cell(act)["workspace_diff_lines"] == 1


def test_a_failed_lint_says_so_with_its_exit_code(tmp_path) -> None:
    result = erosion.lint_status([sys.executable, "-c", "raise SystemExit(3)"], tmp_path)

    assert result == (False, "failed (exit 3)")


def test_a_relative_lint_command_resolves_inside_the_workspace_copy(tmp_path) -> None:
    script = tmp_path / "lint.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)

    assert erosion.lint_status(["./lint.sh"], tmp_path) == (True, None)
    assert erosion.lint_status(["./absent.sh"], tmp_path) == (None, "unavailable")


def test_skill_audit_fields_are_none_on_unmeasured_rows() -> None:
    assert run._SKILL_AUDIT_UNMEASURED == dict.fromkeys(
        ("skill_calls", "non_plugin_skill_calls", "bundled_skills_exposed")
    )


def test_a_new_self_ignoring_gitignore_does_not_hide_its_directory(cell) -> None:
    (cell.source / "keep.txt").write_text("k\n")

    def act(cwd: Path) -> None:
        (cwd / "newpkg").mkdir()
        (cwd / "newpkg" / ".gitignore").write_text("*\n")  # 1 added
        (cwd / "newpkg" / "mod.py").write_text("a\nb\n")  # 2 added

    assert cell(act)["workspace_diff_lines"] == 3


def test_a_nested_git_repo_counts_its_working_files_but_not_its_git_dir(cell) -> None:
    (cell.source / "keep.txt").write_text("k\n")

    def act(cwd: Path) -> None:
        (cwd / "vendored").mkdir()
        (cwd / "vendored" / "lib.py").write_text("1\n2\n3\n")  # 3 added
        assert erosion._RUN(["git", "init", "-q", str(cwd / "vendored")]).returncode == 0  # real git

    assert cell(act)["workspace_diff_lines"] == 3


def test_a_pre_existing_ignored_cache_dir_stays_excluded_after_new_gitignores(cell) -> None:
    (cell.source / ".gitignore").write_text(".mypy_cache/\n")

    def act(cwd: Path) -> None:
        (cwd / ".mypy_cache").mkdir()
        (cwd / ".mypy_cache" / ".gitignore").write_text("*\n")
        (cwd / ".mypy_cache" / "meta.json").write_text("{}\n" * 50)
        (cwd / "new.txt").write_text("n\n")  # 1 added

    assert cell(act)["workspace_diff_lines"] == 1


def _measure(tmp_path: Path, setup, act) -> int | None:
    work = tmp_path / "w"
    work.mkdir()
    setup(work)
    with erosion.snapshot(work) as baseline:
        assert baseline is not None
        act(work)
        return erosion.workspace_diff_lines(baseline, work)


def _tool_folders(work: Path) -> None:
    (work / "tools").mkdir()
    (work / "tools" / "pyvenv.cfg").write_text("home = /usr\n")  # a virtualenv under any name
    for i in range(30):
        (work / "tools" / f"mod{i}.py").write_text("x = 1\n" * 10)
    (work / ".ruff_cache").mkdir()
    (work / ".ruff_cache" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (work / ".ruff_cache" / "entry").write_text("c\n" * 40)
    (work / "cache2").mkdir()
    (work / "cache2" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (work / "cache2" / "entry").write_text("c\n" * 40)
    (work / "pkg.egg-info").mkdir()
    (work / "pkg.egg-info" / "PKG-INFO").write_text("x\n" * 20)
    (work / "node_modules" / "dep").mkdir(parents=True)
    (work / "node_modules" / "dep" / "index.js").write_text("y\n" * 20)


def test_tool_folders_the_agent_creates_add_no_lines_but_real_source_does(tmp_path) -> None:
    def setup(work: Path) -> None:
        (work / "keep.txt").write_text("k\n")

    def act(work: Path) -> None:
        _tool_folders(work)
        (work / "real.py").write_text("a\nb\n")  # 2 added

    assert _measure(tmp_path, setup, act) == 2


def test_a_self_ignoring_gitignore_in_an_ordinary_dir_still_counts_beside_tool_folders(tmp_path) -> None:
    def act(work: Path) -> None:
        _tool_folders(work)
        (work / "newpkg").mkdir()
        (work / "newpkg" / ".gitignore").write_text("*\n")  # 1 added
        (work / "newpkg" / "mod.py").write_text("a\nb\n")  # 2 added

    assert _measure(tmp_path, lambda work: (work / "keep.txt").write_text("k\n"), act) == 3


def test_a_tool_folder_present_before_the_agent_runs_diffs_to_zero(tmp_path) -> None:
    assert _measure(tmp_path, _tool_folders, lambda work: None) == 0


def test_an_untouched_pre_existing_nested_repo_diffs_to_zero(tmp_path) -> None:
    def setup(work: Path) -> None:
        (work / "keep.txt").write_text("k\n")
        (work / "vendored").mkdir()
        (work / "vendored" / "lib.py").write_text("1\n2\n3\n")
        assert erosion._RUN(["git", "init", "-q", str(work / "vendored")]).returncode == 0

    assert _measure(tmp_path, setup, lambda work: None) == 0


@pytest.mark.parametrize("name", [":!x.py", ":(exclude)x", ":_x"])
def test_pathspec_magic_file_names_are_measured_and_counted(tmp_path, name) -> None:
    def act(work: Path) -> None:
        (work / name).write_text("a\nb\n")  # 2 added

    assert _measure(tmp_path, lambda work: (work / "keep.txt").write_text("k\n"), act) == 2
