"""Evolve's one-shot proposer repair and the failure tail that feeds it.

No test calls claude, codex, or cargo: the proposer is a stub that returns scripted diffs, and ``just check`` is a
stub that fails any worktree whose ``src/lib.rs`` holds the word BROKEN.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import candidate as candidates
from evolve import directives, engine
from evolve.loop import TAIL_LINES, tail
from evolve_support import build, log_text, seed_candidate
from test_evolve import patch_for, ready, world  # noqa: F401  (world is a fixture)

SRC = candidates.SRC_PATCH
READ_MD = "prompts/tools/read.md"
CLIPPY_ERROR = """\
error: this `map_or` can be simplified
   --> src/search/mod.rs:797:21
    |
797 | /                     best.map_or(true, |(bs, be)| {
798 | |                         let better = span_len(start, end) < span_len(bs, be);
799 | |                         better
800 | |                     })
    | |______________________^
    |
    = help: for further information visit https://rust-lang.github.io/rust-clippy/master/index.html#unnecessary_map_or
    = note: `-D clippy::unnecessary-map-or` implied by `-D warnings`
help: use is_none_or instead
    |
797 ~                     best.is_none_or(|(bs, be)| {
    |
"""


def cargo_output(error: str = CLIPPY_ERROR) -> str:
    compiling = "".join(f"   Compiling crate{n} v0.{n}.0\n" for n in range(60))
    checking = "".join(f"    Checking dep{n} v1.{n}.0\n" for n in range(30))
    # Other jobs keep running after the first error, so the block is far from the end.
    after = "".join(f"   Compiling late{n} v2.{n}.0\n" for n in range(45))
    return (f"cargo clippy --all-targets -- -D warnings\n{compiling}{checking}{error}{after}"
            "error: could not compile `tilth` (lib) due to 1 previous error\n"
            "warning: build failed, waiting for other jobs to finish...\n"
            "error: could not compile `tilth` (lib test) due to 1 previous error\n"
            "error: Recipe `check` failed on line 12 with exit code 101\n")


# --- the failure tail ---


def test_the_tail_keeps_the_first_error_block_of_a_long_cargo_output() -> None:
    shown = tail(cargo_output())

    assert "error: this `map_or` can be simplified" in shown
    assert "--> src/search/mod.rs:797:21" in shown
    assert "unnecessary_map_or" in shown
    assert "Recipe `check` failed" in shown
    assert len(shown.splitlines()) <= TAIL_LINES
    assert "Compiling crate3 " not in shown


def test_the_tail_keeps_every_error_block_within_the_bound() -> None:
    second = "error[E0425]: cannot find value `x` in this scope\n --> src/edit/apply.rs:42:9\n  |\n42 |     x\n  |     ^ not found\n"
    shown = tail(cargo_output(CLIPPY_ERROR + "\n" + second))

    assert "map_or" in shown
    assert "error[E0425]" in shown
    assert "--> src/edit/apply.rs:42:9" in shown
    assert len(shown.splitlines()) <= TAIL_LINES


def test_a_short_output_and_an_output_without_error_blocks_keep_the_last_lines() -> None:
    assert tail("a\nb\n") == "a\nb"
    plain = "\n".join(f"line {n}" for n in range(100))

    assert tail(plain).splitlines() == plain.splitlines()[-TAIL_LINES:]


# --- repair ---


def broken_patch(world) -> str:
    return patch_for(world, {"src/lib.rs": "pub fn lib_marker() -> u8 {\n    BROKEN\n}\n"})


def fixed_patch(world) -> str:
    return patch_for(world, {"src/lib.rs": "pub fn lib_marker() -> u8 {\n    2\n}\n"})


class Harness:
    """An evolution with a scripted proposer and a ``just check`` that fails any worktree holding BROKEN."""

    def __init__(self, world, *extra: str, patches: list[str]) -> None:
        self.world = world
        self.evo = ready(world, *extra)
        self.patches = list(patches)
        self.calls: list[tuple[dict, list[dict]]] = []
        self.checked: list[Path] = []
        self.evo.dispatcher.propose = self.propose
        self.evo.just_check = self.just_check

    def propose(self, candidate, records) -> str:
        self.calls.append((dict(candidate), records))
        return self.patches.pop(0)

    def just_check(self, worktree: Path) -> tuple[bool, str]:
        self.checked.append(worktree)
        if "BROKEN" in (worktree / "src" / "lib.rs").read_text():
            return False, cargo_output()
        return True, "just check: ok"

    def dispatch(self, names=(SRC,)) -> dict[str, str]:
        parent = seed_candidate()
        return self.evo.dispatcher(parent, {name: [{"records": [{"kind": "fixture"}]}] for name in names},
                                   list(names))


def repair_records(harness: Harness) -> list[dict]:
    return [record for _candidate, records in harness.calls for record in records if "repair" in record]


def test_a_failing_first_attempt_gets_one_repair_call_with_the_error(world) -> None:  # noqa: F811
    broken, fixed = broken_patch(world), fixed_patch(world)
    harness = Harness(world, patches=[broken, fixed])

    proposal = harness.dispatch()

    assert len(harness.calls) == 2
    (_, first_records), (repair_base, repair_call) = harness.calls
    assert not any("repair" in record for record in first_records)
    assert repair_base[SRC] == broken
    [record] = [record for record in repair_call if "repair" in record]
    assert record["repair"]["stage"] == "just check"
    assert "error: this `map_or` can be simplified" in record["repair"]["error"]
    assert record["repair"]["instruction"] == "Fix only this failure; keep the intended change."
    assert proposal == {SRC: fixed}


def test_the_repair_error_names_no_worktree_path_or_forbidden_term(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[broken_patch(world), fixed_patch(world)])

    def leaky_check(worktree: Path) -> tuple[bool, str]:
        harness.checked.append(worktree)
        if "BROKEN" in (worktree / "src" / "lib.rs").read_text():
            return False, f"error: this `map_or` can be simplified\n   --> {worktree}/src/lib.rs:2:5\nsee benchmark/results"
        return True, "just check: ok"

    harness.evo.just_check = leaky_check
    harness.dispatch()

    [record] = repair_records(harness)
    error = record["repair"]["error"]
    assert "--> src/lib.rs:2:5" in error, error
    assert "/worktrees/" not in error and "benchmark/" not in error, error

def test_a_repair_call_that_raises_keeps_the_failed_patch_and_the_run_going(world) -> None:  # noqa: F811
    broken = broken_patch(world)
    harness = Harness(world, patches=[broken])

    def propose(candidate, records):
        harness.calls.append((dict(candidate), records))
        if any("repair" in record for record in records):
            raise RuntimeError("proposer crashed")
        return broken

    harness.evo.dispatcher.propose = propose
    proposal = harness.dispatch()

    assert proposal == {SRC: broken}
    assert len(harness.calls) == 2

def test_the_repaired_child_is_evaluated_in_place_of_the_broken_one_without_a_second_check(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[broken_patch(world), fixed_patch(world)])
    proposal = harness.dispatch()
    evo = harness.evo
    child = {**seed_candidate(), **proposal}

    runs = len(harness.checked)  # the broken child and the repaired child, each once
    cascade = evo.new_cascade(child)
    assert evo.precheck(child) is None
    assert cascade.just_check_ok is True
    assert evo.precheck(child) is None

    assert runs == 2 and len(harness.checked) == runs
    assert candidates.content_id(child) in evo.results


def test_a_repair_that_also_fails_counts_as_failed_with_both_attempts_logged(world) -> None:  # noqa: F811
    still_broken = patch_for(world, {"src/lib.rs": "pub fn lib_marker() -> u8 {\n    STILL_WRONG\n}\n"})
    harness = Staged(world, {"BROKEN": cargo_output(), "STILL_WRONG": TEST_FAILURE}, [broken_patch(world), still_broken])

    proposal = harness.dispatch()
    final = {**seed_candidate(), **proposal}
    failure = harness.evo.precheck(final)

    assert len(harness.calls) == 2
    assert failure is not None and failure[0] == "just check"
    log = log_text(world)
    assert log.count(": failed at just check") == 2
    assert harness.evo.results[candidates.content_id(final)].stage == "just check"
    assert "error: this `map_or` can be simplified" in log


def test_a_passing_first_attempt_makes_no_repair_call(world) -> None:  # noqa: F811
    fixed = fixed_patch(world)
    harness = Harness(world, patches=[fixed])

    proposal = harness.dispatch()

    assert len(harness.calls) == 1
    assert not repair_records(harness)
    assert proposal == {SRC: fixed}


def test_an_apply_failure_is_repaired_with_the_failed_diff_as_text(world) -> None:  # noqa: F811
    garbage = "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1,1 +1,1 @@\n-nomatch\n+x\n"
    fixed = fixed_patch(world)
    harness = Harness(world, patches=[garbage, fixed])

    proposal = harness.dispatch()

    [record] = repair_records(harness)
    assert record["repair"]["stage"] == "apply"
    assert record["repair"]["failed_diff"] == garbage
    assert harness.calls[1][0][SRC] == ""
    assert proposal == {SRC: fixed}


def test_no_repair_disables_the_second_call_and_the_precheck(world) -> None:  # noqa: F811
    harness = Harness(world, "--no-repair", patches=[broken_patch(world)])

    proposal = harness.dispatch()

    assert harness.evo.settings.repair is False
    assert len(harness.calls) == 1
    assert harness.checked == []
    assert proposal == {SRC: broken_patch(world)}


def test_repair_is_on_by_default(world) -> None:  # noqa: F811
    assert build(world).settings.repair is True


def test_a_text_component_is_never_repaired(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[])
    harness.evo.dispatcher.reflect = lambda name, text, records: text + " changed"

    proposal = harness.dispatch(names=(READ_MD,))

    assert proposal[READ_MD].endswith(" changed")
    assert harness.calls == []
    assert harness.checked == []


def test_the_directive_is_consumed_once_across_both_attempts(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[broken_patch(world), fixed_patch(world)])
    queue = directives.DirectiveQueue([directives.Directive("d1", SRC, "change it", None)], lambda line: None)
    queue.take(1)
    harness.evo.dispatcher.directives = queue

    harness.dispatch()

    marked = [any("directive" in record for record in records) for _candidate, records in harness.calls]
    assert marked == [True, True]
    assert queue.claim() is None


def test_the_hypothesis_attaches_to_the_final_child(world) -> None:  # noqa: F811
    broken, fixed = broken_patch(world), fixed_patch(world)
    harness = Harness(world, patches=[broken, fixed])
    hypothesis = {"metric": "calls_per_cell", "scope": "tilth_search", "direction": "decrease", "tolerance": 0.1,
                  "invariants": ["accuracy does not drop"]}
    original = harness.propose

    def propose(candidate, records):
        patch = original(candidate, records)
        harness.evo.hypotheses.offer(SRC, f"<hypothesis>{json.dumps(hypothesis)}</hypothesis>")
        return patch

    harness.evo.dispatcher.propose = propose
    proposal = harness.dispatch()

    stored = [json.loads(line) for line in (harness.evo.run_dir / "hypotheses.jsonl").read_text().splitlines()]
    [record] = [record for record in stored if record["kind"] == "hypothesis"]
    assert record["child"] == candidates.content_id({**seed_candidate(), **proposal})


def test_dispatcher_without_a_check_hook_never_repairs() -> None:
    seen: list = []
    dispatcher = engine.Dispatcher(engine.StopState(), reflect=lambda n, t, r: t,
                                   propose=lambda c, r: seen.append(r) or "patch")

    assert dispatcher(seed_candidate(), {SRC: [{}]}, [SRC]) == {SRC: "patch"}
    assert len(seen) == 1


def test_the_stop_state_skips_the_check_and_the_repair() -> None:
    stop = engine.StopState()
    checks: list = []
    dispatcher = engine.Dispatcher(stop, reflect=lambda n, t, r: t, propose=lambda c, r: stop.set("quota") or "patch",
                                   check=lambda child: checks.append(child) or ("just check", "boom"))

    assert dispatcher(seed_candidate(), {SRC: [{}]}, [SRC]) == {}
    assert checks == []


def test_an_unchanged_patch_is_not_checked_again(world) -> None:  # noqa: F811
    harness = Harness(world, patches=[""])

    assert harness.dispatch() == {SRC: ""}
    assert harness.checked == []
    assert len(harness.calls) == 1


# --- a second repair for lint-only and fmt-only failures ---

TEST_FAILURE = """\
running 1 test
test edit::a ... FAILED

failures:

---- edit::a stdout ----
thread 'edit::a' panicked at src/edit/a.rs:3:5:
assertion `left == right` failed

failures:
    edit::a

test result: FAILED. 0 passed; 1 failed
error: Recipe `check` failed on line 12 with exit code 101
"""
FMT_DIFF = "Diff in src/lib.rs:4:\n-pub fn lib_marker()->u8{\n+pub fn lib_marker() -> u8 {\nerror: Recipe `check` failed on line 3 with exit code 1\n"
COMPILE_ERROR = "error[E0425]: cannot find value `x` in this scope\n --> src/lib.rs:2:5\n  |\n2 |     x\n  |     ^ not found\n"


def marked_patch(world, marker: str) -> str:
    return patch_for(world, {"src/lib.rs": f"pub fn lib_marker() -> u8 {{\n    {marker}\n}}\n"})


class Staged(Harness):
    """A harness whose ``just check`` output depends on the marker word in lib.rs."""

    def __init__(self, world, outputs: dict[str, str], patches: list[str], *extra: str) -> None:
        super().__init__(world, *extra, patches=patches)
        self.outputs = outputs

    def just_check(self, worktree: Path) -> tuple[bool, str]:
        self.checked.append(worktree)
        lib = (worktree / "src" / "lib.rs").read_text()
        for marker, output in self.outputs.items():
            if marker in lib:
                return False, output
        return True, "just check: ok"


@pytest.mark.parametrize("output", [cargo_output(), FMT_DIFF], ids=["clippy", "fmt"])
def test_a_lint_only_repair_failure_gets_a_second_repair_that_is_evaluated(world, output: str) -> None:  # noqa: F811
    first, second, fixed = marked_patch(world, "AAA"), marked_patch(world, "BBB"), fixed_patch(world)
    harness = Staged(world, {"AAA": CLIPPY_ERROR, "BBB": output}, [first, second, fixed])

    proposal = harness.dispatch()

    assert proposal == {SRC: fixed}
    assert len(harness.calls) == 3
    base, records = harness.calls[2]
    assert base[SRC] == second
    [record] = [record for record in records if "repair" in record]
    assert record["repair"]["stage"] == "just check"
    assert ("Diff in src/lib.rs:4" if output == FMT_DIFF else "error: this `map_or` can be simplified") \
        in record["repair"]["error"]
    final = {**seed_candidate(), **proposal}
    assert harness.evo.precheck(final) is None
    log = log_text(world)
    assert "repair 1/2" in log and "repair 2/2" in log


@pytest.mark.parametrize("output", [TEST_FAILURE, COMPILE_ERROR, cargo_output(CLIPPY_ERROR + COMPILE_ERROR)],
                         ids=["test-failure", "compile-error", "lint-and-compile-error"])
def test_a_repair_that_fails_on_more_than_lint_gets_no_second_repair(world, output: str) -> None:  # noqa: F811
    first, second = marked_patch(world, "AAA"), marked_patch(world, "BBB")
    harness = Staged(world, {"AAA": CLIPPY_ERROR, "BBB": output}, [first, second])

    proposal = harness.dispatch()

    assert proposal == {SRC: second}
    assert len(harness.calls) == 2


def test_a_dispatch_makes_at_most_three_proposer_calls(world) -> None:  # noqa: F811
    patches = [marked_patch(world, marker) for marker in ("AAA", "BBB", "CCC")]
    harness = Staged(world, {"AAA": CLIPPY_ERROR, "BBB": CLIPPY_ERROR, "CCC": CLIPPY_ERROR}, patches)

    proposal = harness.dispatch()

    assert proposal == {SRC: patches[2]}
    assert len(harness.calls) == 3
    failure = harness.evo.precheck({**seed_candidate(), **proposal})
    assert failure is not None and failure[0] == "just check"


def test_no_repair_makes_no_lint_repair_either(world) -> None:  # noqa: F811
    first = marked_patch(world, "AAA")
    harness = Staged(world, {"AAA": CLIPPY_ERROR}, [first], "--no-repair")

    assert harness.dispatch() == {SRC: first}
    assert len(harness.calls) == 1


def test_lint_only_reads_every_error_block() -> None:
    clippy_twice = CLIPPY_ERROR + "\n" + CLIPPY_ERROR.replace("map-or", "other")

    assert engine.lint_only(cargo_output())
    assert engine.lint_only(cargo_output(clippy_twice))
    assert engine.lint_only(FMT_DIFF)
    assert engine.lint_only(cargo_output(CLIPPY_ERROR + FMT_DIFF))
    assert not engine.lint_only("")
    assert not engine.lint_only("error: Recipe `check` failed on line 3 with exit code 1\n")
    assert not engine.lint_only(TEST_FAILURE)
    assert not engine.lint_only(cargo_output(COMPILE_ERROR))
    assert not engine.lint_only(cargo_output(CLIPPY_ERROR + COMPILE_ERROR))
    assert not engine.lint_only(cargo_output("error: unused variable: `x`\n  |\n  = note: `-D unused-variables` implied by `-D warnings`\n"))


def test_lint_only_counts_the_same_lint_twice() -> None:
    second = CLIPPY_ERROR.replace("    = note: `-D clippy::unnecessary-map-or` implied by `-D warnings`\n", "")

    assert "-D clippy" not in second
    assert engine.lint_only(cargo_output(CLIPPY_ERROR + "\n" + second))


def test_lint_only_ignores_a_note_written_in_source_text() -> None:
    fake = ("error: unused variable: `x`\n   --> src/a.rs:3:9\n    |\n"
            '3   |     let s = "= note: `-D clippy::fake`";\n    |\n')

    assert not engine.lint_only(cargo_output(fake))
