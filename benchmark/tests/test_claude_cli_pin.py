"""The Claude CLI is pinned: a drifted CLI changes every cell's run key."""

from pathlib import Path

import pytest

import run
from evolve import cli as evolve_cli

_resolve = run.resolve_claude_binary
_require = run.require_pinned_cli


@pytest.fixture
def pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Real resolution against a fake mise root and pin file; returns the pinned binary's path."""
    pin_file = tmp_path / "claude-cli.version"
    pin_file.write_text("2.1.310\n")
    monkeypatch.setattr(run, "CLAUDE_PIN_FILE", pin_file)
    monkeypatch.setattr(run, "MISE_CLAUDE_ROOT", tmp_path / "mise")
    monkeypatch.setattr(run, "resolve_claude_binary", _resolve)
    monkeypatch.setattr(run, "require_pinned_cli", _require)
    monkeypatch.delenv(run.CLAUDE_BINARY_ENV, raising=False)
    binary = tmp_path / "mise" / "v2.1.310" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    return binary


def probe_reporting(monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]) -> list[str]:
    """Stub the version probe by binary path; return the binaries probed."""
    probed: list[str] = []

    def probe(argv: tuple[str, ...]) -> str | None:
        probed.append(argv[0])
        return versions.get(argv[0])

    monkeypatch.setattr(run, "_probe_version", probe)
    monkeypatch.setattr(run, "_run_probe", probe)
    return probed


def test_pin_file_is_a_bare_version() -> None:
    assert run.CLAUDE_PIN_FILE.read_text().strip() == "2.1.310"


def test_pinned_mise_install_is_the_binary(pin: Path) -> None:
    assert run.resolve_claude_binary() == str(pin)


def test_path_claude_is_the_fallback_without_the_mise_install(pin: Path) -> None:
    pin.unlink()
    assert run.resolve_claude_binary() == "claude"


def test_env_override_beats_the_mise_install(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(run.CLAUDE_BINARY_ENV, "/opt/claude")
    assert run.resolve_claude_binary() == "/opt/claude"


def test_cli_version_probes_the_resolved_binary(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probed = probe_reporting(monkeypatch, {str(pin): "2.1.310 (Claude Code)"})
    assert run.cli_version("claude") == "2.1.310 (Claude Code)"
    assert run.cli_version("claude", fresh=True) == "2.1.310 (Claude Code)"
    assert probed == [str(pin), str(pin)]


def test_cli_version_leaves_other_runners_alone(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probed = probe_reporting(monkeypatch, {"codex": "codex 1"})
    assert run.cli_version("codex") == "codex 1"
    assert probed == ["codex"]


def test_matching_version_passes_and_names_the_binary(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_reporting(monkeypatch, {str(pin): "2.1.310 (Claude Code)"})
    line = run.require_pinned_cli()
    assert str(pin) in line and "2.1.310" in line


def test_version_mismatch_refuses_with_found_version_and_fix(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_reporting(monkeypatch, {str(pin): "2.1.311 (Claude Code)"})
    with pytest.raises(run.CliPinError) as error:
        run.require_pinned_cli()
    message = str(error.value)
    assert "2.1.311" in message and "2.1.310" in message
    assert "mise install" in message and run.CLAUDE_BINARY_ENV in message and "--allow-cli-drift" in message


def test_unprobeable_binary_refuses(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_reporting(monkeypatch, {})
    with pytest.raises(run.CliPinError):
        run.require_pinned_cli()


def test_allow_cli_drift_proceeds_and_says_so(pin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_reporting(monkeypatch, {str(pin): "2.1.311 (Claude Code)"})
    assert "--allow-cli-drift" in run.require_pinned_cli(allow_drift=True)


def test_runner_env_disables_the_autoupdater() -> None:
    assert run.build_runner_env("claude", ambient={})["DISABLE_AUTOUPDATER"] == "1"


def test_run_cli_refuses_a_drifted_cli(pin: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    probe_reporting(monkeypatch, {str(pin): "2.1.311 (Claude Code)"})
    monkeypatch.setattr("sys.argv", ["run.py", "--tasks", "all", "--models", "sonnet", "--dry-run"])
    with pytest.raises(SystemExit) as exit_info:
        run.main()
    assert exit_info.value.code == 2
    assert "2.1.311" in capsys.readouterr().err


def test_evolve_refuses_a_drifted_cli(pin: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    probe_reporting(monkeypatch, {str(pin): "2.1.311 (Claude Code)"})
    argv = ["--panel", "p.json", "--max-usd", "1", "--max-metric-calls", "1", "--seed-sha", "HEAD"]
    assert evolve_cli.build(argv) == 2
    assert "2.1.311" in capsys.readouterr().err


def test_evolve_accepts_allow_cli_drift_flag() -> None:
    assert evolve_cli._parser().parse_args(
        ["--panel", "p", "--max-metric-calls", "1", "--seed-sha", "x", "--allow-cli-drift"]).allow_cli_drift
