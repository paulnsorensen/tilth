"""The run.py CLI contract: --output, the --modes-json allowlist and the arm-unavailable family."""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run
from config import ModeConfig
from tasks.base import GroundTruth, Task, TaskSource

FIXTURE_PLUGIN_DIR = str(Path(__file__).parent / "fixtures" / "probe_plugin")


@dataclass
class _CliTask(Task):
    name: str = "cli_task"
    ground_truth: GroundTruth | None = None
    repo: str = "synthetic"
    prompt: str = "Answer the task."
    capability: str = "trace"
    hide_git: bool = False
    source: TaskSource = TaskSource(
        origin="fixture", license="MIT", commit_or_tag="test-pin", transformation="test-only",
    )

    def check_correctness(self, result_text: str, repo_path: str) -> tuple[bool, str]:
        return True, "expected answer"


def _drive_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str, runner=None) -> Path:
    """Run main() on one timed-out cell with no model call; return the results dir."""
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    results = tmp_path / "results"
    monkeypatch.setitem(run.TASKS, "cli_task", _CliTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", results)
    monkeypatch.setattr(run, "reset_repo", lambda: None)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["claude"], 600)

    monkeypatch.setattr(run, "run_single", runner or timeout)
    monkeypatch.setattr(sys, "argv", ["run.py", "--tasks", "cli_task", "--models", "sonnet5",
                                      "--modes", "baseline", "--reps", "1", "--max-cells", "1",
                                      "--max-usd", "100", *extra])
    run.main()
    return results


# --- --output ---


def test_output_writes_rows_to_the_given_path_and_creates_parents(monkeypatch, tmp_path) -> None:
    target = tmp_path / "deep" / "nest" / "rows.jsonl"

    results = _drive_main(monkeypatch, tmp_path, "--output", str(target))

    row = json.loads(target.read_text())
    assert row["error"] == "timeout"
    assert list(results.glob("benchmark_*.jsonl")) == []


def test_output_keeps_stream_logs_beside_the_chosen_file(monkeypatch, tmp_path) -> None:
    target = tmp_path / "out" / "rows.jsonl"

    seen: list[Path] = []

    def timeout(*_args, **kwargs):
        seen.append(kwargs["stream_log_path"])
        raise subprocess.TimeoutExpired(["claude"], 600)

    _drive_main(monkeypatch, tmp_path, "--output", str(target), runner=timeout)

    assert seen and all(path.parent == target.parent / "rows_streams" for path in seen)


def test_default_output_stays_in_the_results_dir(monkeypatch, tmp_path) -> None:
    results = _drive_main(monkeypatch, tmp_path)

    [path] = results.glob("benchmark_*_sonnet5.jsonl")
    assert json.loads(path.read_text())["error"] == "timeout"


def test_output_refuses_an_existing_directory(monkeypatch, tmp_path, capsys) -> None:
    directory = tmp_path / "adir"
    directory.mkdir()

    with pytest.raises(SystemExit) as exit_info:
        _drive_main(monkeypatch, tmp_path, "--output", str(directory))

    assert exit_info.value.code == 2
    assert "is a directory" in capsys.readouterr().err


# --- --modes-json allowlist ---


def _write_modes(tmp_path: Path, **extra) -> Path:
    path = tmp_path / "modes.json"
    path.write_text(json.dumps([{"name": "cli_arm", "tools": ["Read"], "description": "d",
                                 "plugin_dir": FIXTURE_PLUGIN_DIR, **extra}]))
    return path


def test_modes_json_accepts_caller_policy_fields_and_computes_the_rest(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))

    assert run.load_modes_json(_write_modes(tmp_path, prompt_mode="append")) == ["cli_arm"]

    mode = run.MODES["cli_arm"]
    assert mode.prompt_mode == "append"
    assert mode.plugin_sha256 == run._tree_sha256(Path(FIXTURE_PLUGIN_DIR))


@pytest.mark.parametrize("field", [
    "git_sha", "git_ref", "repository", "binary_sha256", "binary_path", "tilth_version", "rustc_version",
    "plugin_version", "plugin_git_sha", "plugin_sha256", "opencode_config_path",
])
def test_modes_json_rejects_harness_computed_fields(monkeypatch, tmp_path, field) -> None:
    monkeypatch.setattr(run, "MODES", dict(run.MODES))
    path = _write_modes(tmp_path, **{field: "forged"})

    with pytest.raises(ValueError, match="harness-computed") as error:
        run.load_modes_json(path)

    assert str(path) in str(error.value)
    assert "cli_arm" in str(error.value)
    assert field in str(error.value)
    assert "cli_arm" not in run.MODES


def test_validate_plugin_arm_stamps_the_tree_digest_and_refuses_mcp(tmp_path) -> None:
    arm = ModeConfig("v", ["Read"], None, "d", plugin_dir=FIXTURE_PLUGIN_DIR)

    stamped = run._validate_plugin_arm(tmp_path / "m.json", arm)

    assert stamped.plugin_sha256 == run._tree_sha256(Path(FIXTURE_PLUGIN_DIR))
    assert arm.plugin_sha256 is None
    with pytest.raises(ValueError, match="serves no MCP server"):
        run._validate_plugin_arm(tmp_path / "m.json", ModeConfig("v", ["Read"], "x.json", "d",
                                                                plugin_dir=FIXTURE_PLUGIN_DIR))


# --- arm-unavailable family and the shared predicate ---


def test_unavailable_errors_share_one_base_with_a_kind() -> None:
    assert issubclass(run.McpUnavailableError, run.ArmUnavailableError)
    assert issubclass(run.SkillUnavailableError, run.ArmUnavailableError)
    assert (run.McpUnavailableError.kind, run.SkillUnavailableError.kind) == ("mcp", "skill")


def test_main_aborts_with_the_kind_specific_message(monkeypatch, tmp_path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setitem(run.TASKS, "cli_task", _CliTask())
    monkeypatch.setattr(run, "SYNTHETIC_REPO", source)
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run, "reset_repo", lambda: None)

    def unavailable(*_args, **_kwargs):
        raise run.SkillUnavailableError("probe:demo missing")

    monkeypatch.setattr(run, "run_single", unavailable)
    monkeypatch.setattr(sys, "argv", ["run.py", "--tasks", "cli_task", "--models", "sonnet5",
                                      "--modes", "baseline", "--reps", "1", "--max-cells", "1",
                                      "--max-usd", "100"])
    with pytest.raises(SystemExit):
        run.main()

    out = capsys.readouterr().out
    assert "SKILL UNAVAILABLE: probe:demo missing" in out
    assert "Aborting run: the skill arm is misconfigured; fix it and re-run." in out
    row = json.loads(next((tmp_path / "results").glob("benchmark_*.jsonl")).read_text())
    assert row["error"] == "skill_unavailable: probe:demo missing"


def test_uses_claude_session_features_covers_skill_and_prompt_arms() -> None:
    plain = ModeConfig("p", ["Read"], None, "d")
    assert run._uses_claude_session_features(plain) is False
    assert run._uses_claude_session_features(ModeConfig("s", ["Read"], None, "d", plugin_dir="/x")) is True
    assert run._uses_claude_session_features(ModeConfig("a", ["Read"], None, "d", prompt_mode="append")) is True
    assert run._uses_claude_session_features(
        ModeConfig("m", ["Read"], "x.json", "d", plugin_dir="/x")) is False
