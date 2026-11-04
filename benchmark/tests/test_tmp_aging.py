"""The tmpfiles.d age-cleanup guard: the ``x`` rule parser, the host probe, and the refusal in ``run.main``."""

import stat
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tmp_aging

# conftest replaces the probe in every test; these tests run the real one against a fake binary.
REAL_CAT_CONFIG = tmp_aging._cat_config
BENCH = PurePosixPath("/tmp/tilth_bench")
FIX = "echo 'x /tmp/tilth_bench' | sudo tee /etc/tmpfiles.d/tilth-bench.conf"
STOCK = """\
# /usr/lib/tmpfiles.d/tmp.conf
q /tmp 1777 root root 10d
q /var/tmp 1777 root root 30d

# /usr/lib/tmpfiles.d/systemd-tmp.conf
x /tmp/systemd-private-%b-*
X /tmp/systemd-private-%b-*/tmp
"""


@pytest.mark.parametrize(("rule", "covered"), [
    ("x /tmp/tilth_bench", True),
    ("x /tmp/tilth_bench*", True),
    ('x "/tmp/tilth_bench"', True),
    ("x /tmp/*", True),
    ("X /tmp/tilth_bench", False),
    ("x! /tmp/tilth_bench", False),
    ("# x /tmp/tilth_bench", False),
    ("#x /tmp/tilth_bench", False),
    ("x /tmp/tilth_bench/repos", False),
    ("x /tmp/tilth_bench_other", False),
    ("x /t*h", False),
    ("d /tmp/tilth_bench 0755 - - -", False),
])
def test_excludes_counts_only_an_x_rule_over_the_path_or_an_ancestor(rule: str, covered: bool) -> None:
    assert tmp_aging.excludes(f"{STOCK}{rule}\n", BENCH) is covered


def test_stock_config_does_not_exclude_the_bench_tree() -> None:
    assert tmp_aging.excludes(STOCK, BENCH) is False


# systemd-tmpfiles keeps the first line for a path and drops a later conflicting one, and a same-path rule with
# an age cleans the contents even when an `x` line names the path too. Both cases lose benchmark files.
@pytest.mark.parametrize(("rules", "covered"), [
    (["X /tmp/tilth_bench", "x /tmp/tilth_bench"], False),
    (["X /tmp/tilth_bench - - - 10d", "x /tmp/tilth_bench"], False),
    (["X /tmp/tilth_bench/", "x /tmp/tilth_bench"], False),
    (["X /tmp/tilth_bench*", "x /tmp/tilth_bench*"], False),
    (["x /tmp/tilth_bench", "d /tmp/tilth_bench 0755 - - 10d"], False),
    (["x /tmp/tilth_bench", "x /tmp/tilth_bench"], True),
    (["X! /tmp/tilth_bench", "x /tmp/tilth_bench"], True),
])
def test_excludes_counts_an_x_rule_only_when_no_other_active_rule_names_its_path(
        rules: list[str], covered: bool) -> None:
    files = "".join(f"# /etc/tmpfiles.d/{n}-bench.conf\n{rule}\n" for n, rule in enumerate(rules))

    assert tmp_aging.excludes(f"{STOCK}{files}", BENCH) is covered


def fake_tmpfiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "systemd-tmpfiles"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(tmp_aging, "_cat_config", REAL_CAT_CONFIG)


def test_require_exclusion_refuses_with_the_one_line_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tmpfiles(tmp_path, monkeypatch, "echo 'q /tmp 1777 root root 10d'")

    with pytest.raises(tmp_aging.TmpAgingError) as error:
        tmp_aging.require_exclusion(BENCH)

    assert FIX in str(error.value)


def test_require_exclusion_passes_when_the_host_config_excludes_the_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tmpfiles(tmp_path, monkeypatch, "echo 'q /tmp 1777 root root 10d'; echo 'x /tmp/tilth_bench'")

    tmp_aging.require_exclusion(BENCH)


def test_require_exclusion_refuses_when_cat_config_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tmpfiles(tmp_path, monkeypatch, "echo 'unknown option' >&2; exit 1")

    with pytest.raises(tmp_aging.TmpAgingError, match="exited 1: unknown option"):
        tmp_aging.require_exclusion(BENCH)


def test_require_exclusion_skips_a_host_without_systemd_tmpfiles(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tmp_aging, "_cat_config", REAL_CAT_CONFIG)

    tmp_aging.require_exclusion(BENCH)


def test_require_exclusion_skips_a_tree_outside_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmp_aging, "_cat_config", lambda: pytest.fail("probed a tree outside /tmp"))

    tmp_aging.require_exclusion(PurePosixPath("/srv/tilth_bench"))


# --- wiring through run.main ---


def test_main_refuses_a_paid_run_before_any_cell_or_spend(bench, monkeypatch: pytest.MonkeyPatch, capsys,
                                                          tmp_path: Path) -> None:
    monkeypatch.setattr(tmp_aging, "_cat_config", lambda: STOCK)
    bench.runner(lambda _stream: pytest.fail("model call started"))

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "plain", "--reps", "1",
                      "--max-usd", "5")

    assert code == 2 and bench.calls == []
    assert FIX in capsys.readouterr().err
    assert list(tmp_path.joinpath("bench-data").rglob("*.jsonl")) == []
    assert not bench.results_dir.exists() or list(bench.results_dir.glob("benchmark_*.jsonl")) == []


def test_dry_run_skips_the_host_check(bench, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(tmp_aging, "_cat_config", lambda: pytest.fail("dry run probed tmpfiles"))

    code = bench.main("--tasks", "cell_a", "--models", "sonnet5", "--modes", "plain", "--reps", "1", "--dry-run")

    assert code == 0 and "1 would call a model" in capsys.readouterr().out


def test_evolve_refuses_before_any_spend(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from evolve import cli as evolve_cli

    monkeypatch.setattr(tmp_aging, "_cat_config", lambda: STOCK)
    argv = ["--panel", "p.json", "--max-usd", "1", "--max-metric-calls", "1", "--seed-sha", "HEAD"]

    assert evolve_cli.build(argv) == 2
    assert FIX in capsys.readouterr().err
