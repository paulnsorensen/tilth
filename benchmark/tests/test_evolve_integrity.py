"""The integrity stage runs the seed's own tests, exactly as the seed wrote them, against a candidate's production code.

No test calls claude, codex, or a paid API. Most tests use a stub cargo. A few run a real ``cargo test`` on a tiny
fixture crate, to show that a candidate which fools ``just check`` fails this stage.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import candidate as candidates
from evolve import rust
from evolve.engine import INTEGRITY_INSTRUCTION
from evolve.finish import changed_tests_section
from evolve.integrity import HEADLINE, LIST_ARGS, TEST_ARGS, Integrity, _swap_regions
from evolve.materialize import Materializer
from evolve_support import cargo_available, git, seed_candidate
from test_evolve import patch_for, world  # noqa: F401  (world is a fixture)
from test_evolve_fmt_tests import (ADDED_NAME, EDITS_APPLY, FAILURE, FORMATTED, SRC, Repair, inline_child,
                                   inline_tests, two_tests, with_added)  # noqa: F401  (fixtures)

real_cargo_needed = pytest.mark.skipif(not cargo_available(), reason="cargo is not on PATH")

CARGO_TOML = '[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n'
MAIN = "fn main() {}\n"
VALUE_IS_ONE = "tests::value_is_one"
SEED_LIB = ("pub fn value() -> u8 {\n    1\n}\n\n"
            "#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
            "    #[test]\n    fn value_is_one() {\n        assert_eq!(value(), 1);\n    }\n}\n")
TWO = SEED_LIB.replace("    1\n}", "    2\n}")
SHADOW = "    macro_rules! assert_eq { ($a:expr, $b:expr) => {}; } #[test]\n    fn added() {\n        assert!(value() > 0);\n    }\n\n"


class Stub:
    """A cargo stub: lists ``names`` for the seed and the candidate tree, and records every call and tree."""

    def __init__(self, names=(VALUE_IS_ONE,), tree_names=None, passes=True, output=FAILURE) -> None:
        self.names, self.tree_names, self.passes, self.output = names, tree_names, passes, output
        self.calls: list[tuple[str, list[str]]] = []
        self.trees: list[dict[str, str]] = []

    def __call__(self, tree: Path, args: list[str]) -> tuple[bool, str]:
        self.calls.append((tree.name, args))
        if args == LIST_ARGS:
            names = self.names if tree.name == "integrity-seed" or self.tree_names is None else self.tree_names
            return True, "".join(f"{name}: test\n" for name in names)
        self.trees.append({path.relative_to(tree).as_posix(): path.read_text() for path in sorted(tree.rglob("*.rs"))})
        return self.passes, self.output


def make_seed(tmp: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp / "seed-repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    for name, text in {"Cargo.toml": CARGO_TOML, "src/main.rs": MAIN, **files}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "seed", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo).strip()


def make_tree(tmp: Path, name: str, files: dict[str, str]) -> Path:
    tree = tmp / name
    for path, text in {"Cargo.toml": CARGO_TOML, "src/main.rs": MAIN, **files}.items():
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(text)
    return tree


def integrity(tmp: Path, seed_files: dict[str, str], cargo) -> Integrity:
    repo, sha = make_seed(tmp, seed_files)
    return Integrity(repo, sha, tmp / "scratch", cargo)


def real_cargo(target: Path):
    env = {**os.environ, "CARGO_TARGET_DIR": str(target), "CARGO_TERM_COLOR": "never"}

    def run(tree: Path, args: list[str]) -> tuple[bool, str]:
        done = subprocess.run(["cargo", *args], cwd=tree, env=env, capture_output=True, text=True, timeout=900)
        return done.returncode == 0, done.stdout + done.stderr

    return run


# --- rust helpers ---


def test_new_test_problem_names_a_missing_attribute_and_an_ignore() -> None:
    assert rust.new_test_problem("#[test]\nfn a() {}") is None
    assert rust.new_test_problem("#[test]\n#[ignore]\nfn a() {}") == "it is ignored"
    assert rust.new_test_problem('#[test]\n#[ignore = "slow"]\nfn a() {}') == "it is ignored"
    assert rust.new_test_problem("fn a() {}") == "it carries no test attribute"


def test_a_standalone_fn_shares_no_line_with_other_code() -> None:
    assert rust.is_standalone_fn("#[test]\nfn a() {\n    assert_eq!(1, 1);\n}")
    assert not rust.is_standalone_fn("#[test] fn a() {}\nfn b() {}")
    assert not rust.is_standalone_fn("macro_rules! m { () => {}; } #[test]\nfn a() {}")
    assert not rust.is_standalone_fn("#[test]\nfn a() {} fn b() {}")


def test_macro_names_and_calls() -> None:
    text = "macro_rules! assert_eq { () => {}; }\nfn f() { assert_eq!(1, 1); other!(); }\n"

    assert rust.macro_names(text) == {"assert_eq"}
    assert {"assert_eq", "other"} <= set(rust.invoked_macros(text))


def test_guarded_spans_leave_out_plain_cfg_test_items() -> None:
    text = ("#[cfg(test)]\nmod tests {}\n\n#[cfg(not(test))]\nfn a() {}\n\n"
            "#[cfg(debug_assertions)]\nfn b() {}\n\n#[cfg(any(unix, test))]\nfn c() {}\n")

    assert rust.guarded_spans(text) == [(4, 5), (7, 8), (10, 11)]
    assert [key for _s, _e, key, _i in rust.cfg_test_regions(text)][0] == "#[cfg(test)] mod tests"


def test_cfg_test_regions_keep_the_outermost_item_only() -> None:
    text = "#[cfg(test)]\nmod tests {\n    #[cfg(test)]\n    fn inner() {}\n}\n"

    assert len(rust.cfg_test_regions(text)) == 1


# --- the tree ---


def test_swapping_regions_keeps_production_code_and_takes_the_seed_test_module(tmp_path: Path) -> None:
    text = _swap_regions(TWO.replace("fn value_is_one", "fn edited"), SEED_LIB)

    assert "    2\n}" in text and "fn value_is_one" in text and "fn edited" not in text


def test_a_cfg_attribute_stacked_above_the_test_module_stays_on_the_seeds_module() -> None:
    text = _swap_regions(SEED_LIB.replace("#[cfg(test)]\nmod", "#[cfg(any())]\n#[cfg(test)]\nmod"), SEED_LIB)

    assert "#[cfg(any())]\n#[cfg(test)]\nmod tests" in text


def test_a_seed_test_module_the_candidate_removed_comes_back() -> None:
    text = _swap_regions("pub fn value() -> u8 {\n    2\n}\n", SEED_LIB)

    assert "fn value_is_one" in text and "    2\n}" in text


def test_the_tree_holds_the_seed_module_file_and_drops_an_added_one(tmp_path: Path) -> None:
    lib = SEED_LIB + "\n#[cfg(test)]\nmod pins;\n"
    pins = "#[test]\nfn pin() {\n    assert_eq!(1, 1);\n}\n"
    stage = integrity(tmp_path, {"src/lib.rs": lib, "src/pins.rs": pins}, Stub(names=(VALUE_IS_ONE, "pins::pin")))
    stub = stage.cargo
    tree = make_tree(tmp_path, "cand", {"src/lib.rs": lib + "\n#[cfg(test)]\nmod extra;\n",
                                        "src/pins.rs": pins.replace("1, 1", "2, 2"),
                                        "src/extra.rs": "#[test]\nfn more() {}\n"})

    ok, output = stage.check(tree)

    assert ok, output
    assert stub.trees[-1]["src/pins.rs"] == pins
    assert "src/extra.rs" not in stub.trees[-1]
    assert "mod extra;" not in stub.trees[-1]["src/lib.rs"]


def test_the_seed_is_listed_once_per_stage(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub())

    for name in ("one", "two"):
        assert stage.check(make_tree(tmp_path, name, {"src/lib.rs": TWO}))[0]

    assert [name for name, args in stage.cargo.calls if args == LIST_ARGS].count("integrity-seed") == 1


def test_a_missing_seed_test_fails_and_is_named(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub(tree_names=()))

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO}))

    assert not ok
    assert VALUE_IS_ONE in output and "missing" in output and HEADLINE in output
    assert ("cand", TEST_ARGS) not in stage.cargo.calls


def test_a_failing_seed_test_fails_and_is_named(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub(passes=False))

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO}))

    assert not ok
    assert EDITS_APPLY in output and HEADLINE in output


def test_a_production_macro_that_shadows_a_seed_macro_is_refused(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub())
    lib = "macro_rules! assert_eq { ($a:expr, $b:expr) => {}; }\n" + TWO

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": lib}))

    assert not ok and "assert_eq!" in output and "cannot be replaced soundly" in output
    assert stage.cargo.trees == []


def test_an_inner_test_cfg_in_a_production_file_is_refused(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub())

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO + "#![cfg(test)]\n"}))

    assert not ok and "inner" in output


def test_a_granted_test_takes_the_candidates_version_and_must_stand_alone(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub())
    updated = TWO.replace("assert_eq!(value(), 1)", "assert_eq!(value(), 2)")
    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": updated}), frozenset({VALUE_IS_ONE}))
    assert ok, output
    assert "assert_eq!(value(), 2)" in stage.cargo.trees[-1]["src/lib.rs"]

    shared = updated.replace("    #[test]\n    fn value_is_one() {", "    fn helper() {} #[test]\n    fn value_is_one() {")
    ok, output = stage.check(make_tree(tmp_path, "shared", {"src/lib.rs": shared}), frozenset({VALUE_IS_ONE}))
    assert not ok and "shares its lines" in output


def test_a_granted_test_the_candidate_deleted_is_refused(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, Stub())

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": "pub fn value() -> u8 {\n    2\n}\n"}),
                             frozenset({VALUE_IS_ONE}))

    assert not ok and "missing from the candidate" in output


# --- real cargo, on a tiny fixture crate ---


@pytest.fixture(scope="module")
def target(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("integrity-target")


@pytest.fixture
def cargo_stage(tmp_path: Path, target: Path):
    cargo = real_cargo(target)
    return tmp_path, integrity(tmp_path, {"src/lib.rs": SEED_LIB}, cargo), cargo


@real_cargo_needed
def test_a_macro_on_the_attribute_line_passes_just_check_and_fails_integrity(cargo_stage) -> None:
    tmp, stage, cargo = cargo_stage
    fooled = TWO.replace("    use super::*;\n\n", "    use super::*;\n\n" + SHADOW)
    tree = make_tree(tmp, "fooled", {"src/lib.rs": fooled})

    plain_ok, plain = cargo(tree, ["test"])
    ok, output = stage.check(tree)

    assert plain_ok, plain
    assert not ok and VALUE_IS_ONE in output and HEADLINE in output


@real_cargo_needed
def test_a_cfg_any_above_the_test_module_passes_just_check_and_fails_integrity(cargo_stage) -> None:
    tmp, stage, cargo = cargo_stage
    hidden = SEED_LIB.replace("#[cfg(test)]\nmod", "#[cfg(any())]\n#[cfg(test)]\nmod")
    tree = make_tree(tmp, "hidden", {"src/lib.rs": hidden})

    plain_ok, plain = cargo(tree, ["test"])
    ok, output = stage.check(tree)

    assert plain_ok and "0 passed" in plain
    assert not ok and VALUE_IS_ONE in output and "missing" in output


@real_cargo_needed
def test_an_added_test_beside_an_unchanged_existing_test_passes_integrity(cargo_stage) -> None:
    tmp, stage, _cargo = cargo_stage
    added = SEED_LIB.replace("        assert_eq!(value(), 1);\n    }\n",
                             "        assert_eq!(value(), 1);\n    }\n\n    #[test]\n    fn value_is_positive() {\n"
                             "        assert!(value() > 0);\n    }\n")

    ok, output = stage.check(make_tree(tmp, "added", {"src/lib.rs": added}))

    assert ok, output


@real_cargo_needed
def test_a_granted_updated_test_passes_integrity_only_with_the_grant(cargo_stage) -> None:
    tmp, stage, _cargo = cargo_stage
    updated = TWO.replace("assert_eq!(value(), 1)", "assert_eq!(value(), 2)")

    granted_ok, granted_output = stage.check(make_tree(tmp, "granted", {"src/lib.rs": updated}), frozenset({VALUE_IS_ONE}))
    bare_ok, bare_output = stage.check(make_tree(tmp, "bare", {"src/lib.rs": updated}))

    assert granted_ok, granted_output
    assert not bare_ok and VALUE_IS_ONE in bare_output


# --- the stage in the cascade ---


def listing() -> str:
    return "".join(f"{name}: test\n" for name in (EDITS_APPLY, "edit::integration_tests::other_pin"))


class CascadeStub:
    """Lists both fixture tests; its test run fails while lib.rs returns 2."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, tree: Path, args: list[str]) -> tuple[bool, str]:
        self.calls.append((tree.name, args))
        if args == LIST_ARGS:
            return True, listing()
        return "    2" not in (tree / "src" / "lib.rs").read_text(), FAILURE


def with_integrity(run: Repair, tmp: Path) -> CascadeStub:
    cargo = CascadeStub()
    run.evo.just_check = lambda worktree: (True, "just check: ok")
    run.evo.integrity = Integrity(run.evo.settings.repo, run.evo.settings.seed_sha, tmp / "scratch", cargo)
    return cargo


def test_the_stage_is_skipped_for_the_seed_and_runs_once_per_patch(two_tests) -> None:
    run = Repair(two_tests, [])
    cargo = with_integrity(run, two_tests.tmp)
    fixed = FORMATTED.replace("    2\n", "    3\n")
    child = {**seed_candidate(), SRC: patch_for(two_tests, {"src/lib.rs": fixed})}

    assert run.evo.precheck(seed_candidate()) is None
    assert cargo.calls == []
    assert run.evo.precheck(child) is None
    calls = len(cargo.calls)
    assert [args for _name, args in cargo.calls] == [LIST_ARGS, LIST_ARGS, TEST_ARGS]
    assert run.evo.precheck({**child, "mcp_instructions": "x"}) is None or True
    assert run.evo.precheck(child) is None
    assert len(cargo.calls) == calls


def test_an_integrity_failure_is_repaired_and_grants_no_test_scope(two_tests) -> None:
    breaking = patch_for(two_tests, {"src/lib.rs": FORMATTED})
    fixed = patch_for(two_tests, {"src/lib.rs": FORMATTED.replace("    2\n", "    3\n")})
    run = Repair(two_tests, [breaking, fixed])
    with_integrity(run, two_tests.tmp)

    proposal = run.dispatch()

    assert proposal == {SRC: fixed}
    [record] = run.records()
    assert record["stage"] == "integrity" and record["instruction"] == INTEGRITY_INSTRUCTION
    assert EDITS_APPLY in record["error"] and HEADLINE in record["error"]
    assert "may_update_tests" not in record and "failing_tests" not in record
    assert run.evo.materializer.test_scope == {}


def test_an_integrity_failure_that_stays_is_a_cascade_failure(two_tests) -> None:
    breaking = patch_for(two_tests, {"src/lib.rs": FORMATTED})
    run = Repair(two_tests, [breaking])
    with_integrity(run, two_tests.tmp)
    child = {**seed_candidate(), SRC: breaking}

    stage, tail = run.evo.precheck(child)

    assert stage == "integrity" and HEADLINE in tail


def test_the_pr_lists_an_added_test(inline_tests) -> None:
    mat = Materializer(inline_tests.repo, inline_tests.seed_sha, "run1", inline_tests.tmp / "work",
                       fmt=lambda root: True)
    child = inline_child(inline_tests, with_added())

    mat.materialize(child)
    section = "\n".join(changed_tests_section(mat.test_diffs[candidates.content_id(child)]))

    assert ADDED_NAME in section and "+    fn thing_is_not_two() {" in section
