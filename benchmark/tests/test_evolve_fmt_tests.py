"""Evolve formats a src_patch child before it commits, and a repair may update the tests its change made obsolete.

No test calls claude, codex, or a paid API. Formatting runs either a stub or a real ``cargo fmt`` on a tiny fixture
crate; ``just check`` is a stub that fails on a marker.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import candidate as candidates
from evolve import proposer, rust
from evolve.finish import TEST_REVIEW_NOTE, GitHubPRClient, changed_tests_section
from evolve.materialize import ApplyRejected, Materializer
from evolve.proposer import PROPOSER_HEADER, REPAIR_NOTE, Proposer
from evolve.rustfmt import format_tree
from evolve_support import (cargo_available, child, git, log_text, make_world, mcp, seed_candidate, seed_files,
                            with_last_line)
from test_evolve import _remote, accept, patch_for, ready, refs, world  # noqa: F401  (world is a fixture)

SRC = candidates.SRC_PATCH
UNFORMATTED = "pub mod mcp;\npub mod edit;\n\npub fn lib_marker()->u8{\n2\n}\n"
UNFORMATTED_FN = "pub fn lib_marker()->u8{\n2\n}\n"
FORMATTED_FN = "pub fn lib_marker() -> u8 {\n    2\n}\n"
FORMATTED = "pub mod mcp;\npub mod edit;\n\npub fn lib_marker() -> u8 {\n    2\n}\n"
TESTS = "src/edit/integration_tests.rs"
EDITS_APPLY = "edit::integration_tests::edits_apply"
OTHER = "edit::integration_tests::other_pin"
TWO_TESTS = ("#[test]\nfn edits_apply() {\n    assert_eq!(1, 1);\n}\n\n"
             "#[test]\nfn other_pin() {\n    assert_eq!(3, 3);\n}\n")
NEW_BEHAVIOR = TWO_TESTS.replace("assert_eq!(1, 1)", "assert_eq!(2, 2)")
fmt_needed = pytest.mark.skipif(not cargo_available(), reason="cargo is not on PATH")

FAILURE = f"""\
running 2 tests
test {OTHER} ... ok
test {EDITS_APPLY} ... FAILED

failures:

---- {EDITS_APPLY} stdout ----
thread panicked at src/edit/integration_tests.rs:3:5:
assertion `left == right` failed

failures:
    {EDITS_APPLY}

test result: FAILED. 1 passed; 1 failed
error: Recipe `check` failed on line 12 with exit code 101
"""


@pytest.fixture
def two_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return make_world(monkeypatch, tmp_path, {**seed_files(), TESTS: TWO_TESTS})


def lib_patch(world, text: str = FORMATTED) -> str:
    return patch_for(world, {"src/lib.rs": text})


def materializer(world, **kwargs) -> Materializer:
    return Materializer(world.repo, world.seed_sha, "run1", world.tmp / "work", **kwargs)


# --- auto-format ---


def rewrite_lib(root: Path) -> bool:
    (root / "src" / "lib.rs").write_text(FORMATTED)
    return True


def test_an_unformatted_child_is_committed_formatted(world) -> None:  # noqa: F811
    sha = materializer(world, fmt=rewrite_lib).materialize({**seed_candidate(), SRC: lib_patch(world, UNFORMATTED)})

    assert git("show", f"{sha}:src/lib.rs", cwd=world.repo) == FORMATTED


def test_the_materializer_formats_after_apply_and_before_the_commit(world) -> None:  # noqa: F811
    seen: list[str] = []

    def watch(root: Path) -> bool:
        seen.append((root / "src" / "lib.rs").read_text())
        return True

    materializer(world, fmt=watch).materialize({**seed_candidate(), SRC: lib_patch(world, UNFORMATTED)})

    assert seen == [UNFORMATTED]


def rustfmt_clean(text: str) -> str:
    done = subprocess.run(["rustfmt", "--emit", "stdout"], input=text, capture_output=True, text=True, check=True)
    return done.stdout


@fmt_needed
def test_just_check_sees_no_fmt_diff_after_a_real_cargo_fmt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean = {name: rustfmt_clean(text) if name.endswith(".rs") else text for name, text in seed_files().items()}
    world = make_world(monkeypatch, tmp_path, clean)
    mat = materializer(world, fmt=format_tree)
    sha = mat.materialize({**seed_candidate(), SRC: lib_patch(world, UNFORMATTED)})

    committed = git("show", f"{sha}:src/lib.rs", cwd=world.repo)
    assert committed == rustfmt_clean(UNFORMATTED) != UNFORMATTED
    checked = subprocess.run(["cargo", "fmt", "--check"], cwd=mat.worktree(sha), capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_a_failing_formatter_keeps_the_patched_tree(world) -> None:  # noqa: F811
    sha = materializer(world, fmt=lambda root: False).materialize(
        {**seed_candidate(), SRC: lib_patch(world, UNFORMATTED)})

    assert git("show", f"{sha}:src/lib.rs", cwd=world.repo) == UNFORMATTED


@fmt_needed
def test_format_tree_leaves_a_file_with_a_parse_error_alone(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (tmp_path / "src").mkdir()
    broken = "pub fn f( -> u8 {\n1\n"
    (tmp_path / "src" / "lib.rs").write_text(broken)

    assert format_tree(tmp_path) is False
    assert (tmp_path / "src" / "lib.rs").read_text() == broken


@fmt_needed
def test_format_tree_formats_a_tiny_crate(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(UNFORMATTED_FN)

    assert format_tree(tmp_path) is True
    assert (tmp_path / "src" / "lib.rs").read_text() == FORMATTED_FN


def test_the_proposers_cumulative_diff_is_formatted_so_the_content_id_is_too(world) -> None:  # noqa: F811
    seen: list[Path] = []

    def fmt(root: Path) -> bool:
        seen.append(root)
        return rewrite_lib(root)

    prop = Proposer(world.repo, world.seed_sha, None, "model", forbidden=[], log=lambda line: None, fmt=fmt)
    with prop.export(seed_candidate()) as root:
        (root / "src" / "lib.rs").write_text(UNFORMATTED)
        diff = prop.cumulative_diff(root)

    assert seen == [root]
    assert "+    2" in diff and "->u8" not in diff
    assert diff == lib_patch(world)
    assert candidates.content_id({**seed_candidate(), SRC: diff}) == candidates.content_id(
        {**seed_candidate(), SRC: lib_patch(world)})


def test_a_failing_formatter_in_the_proposer_is_logged(world) -> None:  # noqa: F811
    lines: list[str] = []
    prop = Proposer(world.repo, world.seed_sha, None, "model", forbidden=[], log=lines.append, fmt=lambda root: False)
    with prop.export(seed_candidate()) as root:
        prop.cumulative_diff(root)

    assert any("cargo fmt failed" in line for line in lines)


@fmt_needed
def test_a_long_last_prompt_line_leaves_no_fmt_diff_after_the_byte_lock_rewrite(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean = {name: rustfmt_clean(text) if name.endswith(".rs") else text for name, text in seed_files().items()}
    world = make_world(monkeypatch, tmp_path, clean)
    mat = materializer(world, fmt=format_tree)
    # Long enough that rustfmt wraps the rewritten `ends_with` assert, short enough to fit once wrapped.
    longer = with_last_line(clean["prompts/mcp.md"], "x" * 54)
    sha = mat.materialize({**seed_candidate(), "prompts/mcp.md": longer})

    assert "x" * 54 in git("show", f"{sha}:src/mcp/mod.rs", cwd=world.repo)
    checked = subprocess.run(["cargo", "fmt", "--check"], cwd=mat.worktree(sha), capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr


# --- rust.py ---


def test_failing_tests_reads_the_failures_list_not_the_stdout_headers() -> None:
    assert rust.failing_tests(FAILURE) == [EDITS_APPLY]
    assert rust.failing_tests("error: this `map_or` can be simplified\n") == []


def test_a_doc_test_name_does_not_end_the_failures_list() -> None:
    output = ("failures:\n    src/x.rs - foo (line 3)\n    edit::a\n    src/y.rs - bar (line 9)\n    edit::b\n\n"
              "test result: FAILED. 0 passed; 4 failed\n    not::a_name\n")

    assert rust.failing_tests(output) == ["edit::a", "edit::b"]


def test_test_functions_name_the_module_path_and_span_each_test() -> None:
    found = rust.test_functions(TESTS, TWO_TESTS)

    assert found == [(EDITS_APPLY, 1, 4), (OTHER, 6, 9)]
    inline = "fn helper() {}\n\nmod t {\n    #[test]\n    fn nested() {}\n}\n"
    assert [name for name, _first, _last in rust.test_functions("src/search/edit.rs", inline)] == [
        "search::edit::t::nested"]


# --- tests in repair only ---


def test_a_non_repair_proposal_may_not_touch_a_test(world) -> None:  # noqa: F811
    patch = patch_for(world, {TESTS: "#[test]\nfn edits_apply() {\n    assert_eq!(2, 2);\n}\n"})

    with pytest.raises(ApplyRejected, match=r"cfg\(test\)"):
        materializer(world, fmt=lambda root: True).materialize({**seed_candidate(), SRC: patch})
    assert refs(world) == []


def granted(mat: Materializer, candidate: dict, *names: str) -> dict:
    mat.test_scope[candidate[SRC]] = frozenset(names)
    return candidate


def test_a_granted_test_may_change_and_its_diff_is_kept(two_tests) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    child = {**seed_candidate(), SRC: patch_for(two_tests, {TESTS: NEW_BEHAVIOR})}

    sha = mat.materialize(granted(mat, child, EDITS_APPLY))

    assert "assert_eq!(2, 2)" in git("show", f"{sha}:{TESTS}", cwd=two_tests.repo)
    [(name, diff)] = mat.test_diffs[candidates.content_id(child)].items()
    assert name == EDITS_APPLY
    assert "-    assert_eq!(1, 1);" in diff and "+    assert_eq!(2, 2);" in diff
    assert "other_pin" not in diff


def test_a_different_test_than_the_granted_one_is_refused(two_tests) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    other = TWO_TESTS.replace("assert_eq!(3, 3)", "assert_eq!(4, 4)")
    child = granted(mat, {**seed_candidate(), SRC: patch_for(two_tests, {TESTS: other})}, EDITS_APPLY)

    with pytest.raises(ApplyRejected, match=r"cfg\(test\)"):
        mat.materialize(child)
    assert refs(two_tests) == []


WEAKENED = {
    "cfg-not-test": "#[cfg(not(test))]\nfn edits_apply() {\n    assert_eq!(2, 2);\n}\n",
    "cfg-any": "#[test]\n#[cfg(any())]\nfn edits_apply() {\n    assert_eq!(2, 2);\n}\n",
    "ignore": "#[test]\n#[ignore]\nfn edits_apply() {\n    assert_eq!(2, 2);\n}\n",
    "should-panic": "#[test]\n#[should_panic]\nfn edits_apply() {\n    assert_eq!(2, 2);\n}\n",
    "two-test-attributes": "#[test]\n#[test]\nfn edits_apply() {\n    assert_eq!(2, 2);\n}\n",
    "empty-body": "#[test]\nfn edits_apply() {}\n",
    "no-assert": "#[test]\nfn edits_apply() {\n    let _unused = 2;\n}\n",
}


@pytest.mark.parametrize("form", sorted(WEAKENED))
def test_a_granted_test_may_not_be_disabled_or_gutted(two_tests, form: str) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    text = WEAKENED[form] + "\n" + TWO_TESTS.split("\n\n", 1)[1]
    child = granted(mat, {**seed_candidate(), SRC: patch_for(two_tests, {TESTS: text})}, EDITS_APPLY)

    with pytest.raises(ApplyRejected, match="weakens the test"):
        mat.materialize(child)
    assert refs(two_tests) == []


@pytest.mark.parametrize("head", ["#[test]", "#[tokio::test]", "#[tokio::test(flavor = \"multi_thread\")]"])
def test_a_granted_test_may_keep_a_test_attribute_of_any_path(two_tests, head: str) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    text = NEW_BEHAVIOR.replace("#[test]\nfn edits_apply", f"{head}\nfn edits_apply")
    child = granted(mat, {**seed_candidate(), SRC: patch_for(two_tests, {TESTS: text})}, EDITS_APPLY)

    assert mat.materialize(child)


def test_a_new_test_beside_the_granted_one_is_refused(two_tests) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    added = NEW_BEHAVIOR + "\n#[test]\nfn extra() {}\n"
    child = granted(mat, {**seed_candidate(), SRC: patch_for(two_tests, {TESTS: added})}, EDITS_APPLY)

    with pytest.raises(ApplyRejected, match=r"cfg\(test\)"):
        mat.materialize(child)


def test_deleting_the_granted_test_is_refused(two_tests) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    only_other = TWO_TESTS.split("\n\n", 1)[1]
    child = granted(mat, {**seed_candidate(), SRC: patch_for(two_tests, {TESTS: only_other})}, EDITS_APPLY)

    with pytest.raises(ApplyRejected, match="deletes the failing test"):
        mat.materialize(child)


def test_a_grant_does_not_open_code_outside_the_test(two_tests) -> None:
    mat = materializer(two_tests, fmt=lambda root: True)
    for name, text in (
        ("src/lib.rs", None),
        ("benchmark/run.py", "# changed\n"),
    ):
        changes = {TESTS: NEW_BEHAVIOR, name: text or "pub fn lib_marker() -> u8 {\n    9\n}\n"}
        child = granted(mat, {**seed_candidate(), SRC: patch_for(two_tests, changes)}, EDITS_APPLY)
        if name == "src/lib.rs":
            mat.materialize(child)  # non-test code in the same patch stays allowed
        else:
            with pytest.raises(ApplyRejected, match=name):
                mat.materialize(child)


class Repair:
    """An evolution with a scripted proposer; ``just check`` fails while lib.rs returns 2 and a test still pins 1."""

    def __init__(self, world, patches: list[str]) -> None:
        self.evo = ready(world)
        self.evo.materializer.fmt = lambda root: True
        self.patches = list(patches)
        self.calls: list[tuple[dict, list[dict]]] = []
        self.evo.dispatcher.propose = self.propose
        self.evo.just_check = self.just_check

    def propose(self, candidate, records) -> str:
        self.calls.append((dict(candidate), records))
        return self.patches.pop(0)

    @staticmethod
    def just_check(worktree: Path) -> tuple[bool, str]:
        lib = (worktree / "src" / "lib.rs").read_text()
        pin = (worktree / TESTS).read_text()
        if "    2" in lib and "assert_eq!(1, 1)" in pin:
            return False, FAILURE
        return True, "just check: ok"

    def dispatch(self, parent: dict | None = None) -> dict[str, str]:
        return self.evo.dispatcher(parent or seed_candidate(), {SRC: [{"records": [{"kind": "fixture"}]}]}, [SRC])

    def records(self) -> list[dict]:
        return [record["repair"] for _candidate, records in self.calls for record in records if "repair" in record]


def test_the_repair_after_failing_tests_names_them_and_may_update_them(two_tests) -> None:
    behavior = patch_for(two_tests, {"src/lib.rs": FORMATTED})
    updated = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR})
    run = Repair(two_tests, [behavior, updated])

    proposal = run.dispatch()

    [repair] = run.records()
    assert repair["stage"] == "just check"
    assert repair["failing_tests"] == [EDITS_APPLY]
    assert repair["may_update_tests"] is True
    assert proposal == {SRC: updated}
    final = {**seed_candidate(), **proposal}
    cascade = run.evo.new_cascade(final)
    assert run.evo.precheck(final) is None
    assert cascade.tests_changed is True
    assert list(run.evo.materializer.test_diffs[candidates.content_id(final)]) == [EDITS_APPLY]
    assert "changes tests for review" in log_text(two_tests) and EDITS_APPLY in log_text(two_tests)


def test_a_repair_that_touches_another_test_fails_the_child(two_tests) -> None:
    behavior = patch_for(two_tests, {"src/lib.rs": FORMATTED})
    wrong = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR.replace("(3, 3)", "(5, 5)")})
    run = Repair(two_tests, [behavior, wrong])

    final = {**seed_candidate(), **run.dispatch()}
    failure = run.evo.precheck(final)

    assert failure is not None and failure[0] == "apply"
    assert "cfg(test)" in failure[1]


def test_a_first_attempt_that_edits_a_test_gets_no_grant(two_tests) -> None:
    first = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR})
    run = Repair(two_tests, [first, first])

    final = {**seed_candidate(), **run.dispatch()}

    assert run.evo.precheck(final) is not None
    assert run.records()[0]["stage"] == "apply"
    assert "failing_tests" not in run.records()[0] and "may_update_tests" not in run.records()[0]


def test_a_descendant_keeps_the_parents_test_grant(two_tests) -> None:
    behavior = patch_for(two_tests, {"src/lib.rs": FORMATTED})
    updated = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR})
    run = Repair(two_tests, [behavior, updated])
    parent = {**seed_candidate(), **run.dispatch()}
    assert run.evo.precheck(parent) is None
    child_patch = patch_for(two_tests, {"src/lib.rs": FORMATTED + "\npub fn more() {}\n", TESTS: NEW_BEHAVIOR})
    run.patches.append(child_patch)

    proposal = run.dispatch(parent)
    child = {**parent, **proposal}

    assert proposal == {SRC: child_patch}
    assert run.evo.precheck(child) is None
    assert list(run.evo.materializer.test_diffs[candidates.content_id(child)]) == [EDITS_APPLY]


def granted_parent(two_tests) -> tuple[Repair, dict[str, str]]:
    """A run whose parent candidate won a repair grant for EDITS_APPLY, and the parent, already materialized."""
    behavior = patch_for(two_tests, {"src/lib.rs": FORMATTED})
    updated = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR})
    run = Repair(two_tests, [behavior, updated])
    parent = {**seed_candidate(), **run.dispatch()}
    assert run.evo.precheck(parent) is None
    return run, parent


def test_a_text_only_child_of_a_granted_parent_keeps_the_grant(two_tests) -> None:
    run, parent = granted_parent(two_tests)
    child = {**parent, "prompts/mcp.md": mcp(dev_a="1", dev_b="1", cheap_a="1", test_a="1")}

    assert run.evo.precheck(child) is None
    assert list(run.evo.materializer.test_diffs[candidates.content_id(child)]) == [EDITS_APPLY]


def test_a_repair_of_a_descendant_keeps_the_inherited_grant_and_adds_the_new_failure(two_tests) -> None:
    run, parent = granted_parent(two_tests)
    broken = patch_for(two_tests, {"src/lib.rs": FORMATTED + "\npub fn broken() {}\n", TESTS: NEW_BEHAVIOR})
    both = NEW_BEHAVIOR.replace("assert_eq!(2, 2)", "assert_eq!(2, 3 - 1)").replace("assert_eq!(3, 3)", "assert_eq!(4, 4)")
    repaired = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: both})
    run.patches[:] = [broken, repaired]
    other_failure = FAILURE.replace(EDITS_APPLY, OTHER)
    run.evo.just_check = lambda worktree: (
        (False, other_failure) if "broken" in (worktree / "src" / "lib.rs").read_text() else (True, "ok"))

    proposal = run.dispatch(parent)
    final = {**parent, **proposal}

    assert proposal == {SRC: repaired}
    assert run.records()[-1]["failing_tests"] == [OTHER]
    assert run.evo.precheck(final) is None
    assert set(run.evo.materializer.test_diffs[candidates.content_id(final)]) == {EDITS_APPLY, OTHER}


def test_a_descendant_without_a_repair_may_not_change_an_inherited_test(two_tests) -> None:
    run, parent = granted_parent(two_tests)
    bad = patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR.replace("(2, 2)", "(7, 7)")})
    run.patches[:] = [bad, bad]  # a repair of an apply failure gets no test grant either

    failure = run.evo.precheck({**parent, **run.dispatch(parent)})

    assert failure is not None and failure[0] == "apply"
    assert "inherited test" in failure[1] and EDITS_APPLY in failure[1]


def test_a_descendant_may_not_revert_an_inherited_test(two_tests) -> None:
    run, parent = granted_parent(two_tests)
    bad = patch_for(two_tests, {"src/lib.rs": FORMATTED + "\npub fn more() {}\n"})
    run.patches[:] = [bad, bad]

    failure = run.evo.precheck({**parent, **run.dispatch(parent)})

    assert failure is not None and failure[0] == "apply"
    assert "no longer carries" in failure[1]


def test_a_check_failure_that_is_not_a_test_gets_no_test_grant(world) -> None:  # noqa: F811
    run = Repair(world, [lib_patch(world), lib_patch(world, FORMATTED.replace("2", "3"))])
    run.just_check = lambda worktree: (False, "error: this `map_or` can be simplified")
    run.evo.just_check = run.just_check

    run.dispatch()

    [repair] = run.records()
    assert "failing_tests" not in repair and "may_update_tests" not in repair


# --- the winner's pull request ---


def test_the_review_section_lists_each_changed_test_with_its_diff() -> None:
    diff = "--- a/x.rs\n+++ b/x.rs\n-    assert_eq!(1, 1);\n+    assert_eq!(2, 2);\n"
    lines = changed_tests_section({EDITS_APPLY: diff})

    text = "\n".join(lines)
    assert "## Test changes for review" in text and TEST_REVIEW_NOTE in text
    assert f"`{EDITS_APPLY}`" in text and diff.strip() in text
    assert changed_tests_section({}) == []


def test_the_prompts_allow_a_test_update_only_for_an_obsolete_pin() -> None:
    assert "except as the repair record allows" in PROPOSER_HEADER
    for phrase in ("may_update_tests", "failing_tests", "obsolete", "NEW", "Never delete", "weaken"):
        assert phrase in REPAIR_NOTE, phrase
    assert proposer.REPAIR_NOTE is REPAIR_NOTE


def test_the_winner_pr_body_lists_the_test_diffs_for_review(two_tests) -> None:
    remote = _remote(two_tests)
    gh: list[list[str]] = []

    def run_gh(argv, **kwargs):
        gh.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "https://example.invalid/pr/7\n", "")

    evo = ready(two_tests, pr_client=GitHubPRClient(two_tests.repo, remote="origin", run=run_gh))
    evo.materializer.fmt = lambda root: True
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    winner = {**child(evo.seed, test_a="1", tag="winner"),
              SRC: patch_for(two_tests, {"src/lib.rs": FORMATTED, TESTS: NEW_BEHAVIOR})}
    evo.materializer.test_scope[winner[SRC]] = frozenset({EDITS_APPLY})
    result = accept(evo, winner, {"dev_a": 0.9, "dev_b": 0.9})
    result.tests_changed = True

    evo.finish()

    [create] = gh
    body = create[create.index("--body") + 1]
    assert "## Test changes for review" in body and TEST_REVIEW_NOTE in body
    assert f"### `{EDITS_APPLY}`" in body
    assert "-    assert_eq!(1, 1);" in body and "+    assert_eq!(2, 2);" in body
    assert "other_pin" not in body
    assert "changes tests for review" in log_text(two_tests)


def test_a_winner_with_no_test_change_has_no_review_section(world) -> None:  # noqa: F811
    remote = _remote(world)
    gh: list[list[str]] = []

    def run_gh(argv, **kwargs):
        gh.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "https://example.invalid/pr/7\n", "")

    evo = ready(world, pr_client=GitHubPRClient(world.repo, remote="origin", run=run_gh))
    accept(evo, evo.seed, {"dev_a": 0.0, "dev_b": 0.0})
    accept(evo, child(evo.seed, test_a="1", tag="winner"), {"dev_a": 0.9, "dev_b": 0.9})
    evo.finish()

    assert "Test changes for review" not in gh[0][gh[0].index("--body") + 1]
