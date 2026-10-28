"""Regression tests for the holes a review found in the integrity stage.

Each test fails on the stage as it stood before the fixes. The ``cargo`` tests run a real ``cargo test`` on a tiny
fixture crate; the others use a stub cargo, or none. No test reaches a model or a paid API.

Names that only the fixed stage has are imported inside the tests, so this file still collects on the old stage.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import rust
from evolve.integrity import Integrity, _swap_regions
from evolve.materialize import HARNESS_TERMS, ApplyRejected, Materializer
from evolve_support import build, cargo_available, git, seed_candidate
from test_evolve import patch_for, world  # noqa: F401  (world is a fixture)
from test_evolve_fmt_tests import FORMATTED, SRC, Repair, two_tests  # noqa: F401  (two_tests is a fixture)
from test_evolve_integrity import SEED_LIB, TWO, VALUE_IS_ONE, integrity, make_seed, make_tree, real_cargo

real_cargo_needed = pytest.mark.skipif(not cargo_available(), reason="cargo is not on PATH")

TEST_MOD = SEED_LIB.split("\n\n", 1)[1]
FAKE_TESTS = "mod tests {\n    #[test]\n    fn value_is_one() {}\n}\n"
TWO_FN = "pub fn value() -> u8 {\n    2\n}\n\n"
OTHER_IS_ONE = "tests::other_is_one"
NESTED_SEED = ("pub trait Validate {\n    fn is_valid(&self) -> bool;\n}\n\npub struct Foo(pub u8);\n\n"
               "impl Validate for Foo {\n    fn is_valid(&self) -> bool {\n        self.0 == 1\n    }\n}\n\n"
               "pub fn value() -> u8 {\n    1\n}\n\npub fn other() -> u8 {\n    1\n}\n\n"
               "pub fn check(v: &dyn Validate) -> bool {\n    v.is_valid()\n}\n\n"
               "#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
               "    #[test]\n    fn value_is_valid() {\n        assert!(Foo(value()).is_valid());\n    }\n\n"
               "    #[test]\n    fn other_is_one() {\n        assert_eq!(other(), 1);\n    }\n}\n")
NESTED_IMPL = ("        #[allow(non_local_definitions)]\n        impl Foo {\n            pub fn is_valid(&self) -> bool {\n"
               "                true\n            }\n        }\n")
OTHER_BODY = "    fn other_is_one() {\n        assert_eq!(other(), 1);\n    }"
PATH_SEED = {"src/lib.rs": "pub fn value() -> u8 {\n    1\n}\n\n#[cfg(test)]\nmod tests;\n",
             "src/tests.rs": "use super::*;\n\n#[test]\nfn value_is_one() {\n    assert_eq!(value(), 1);\n}\n"}


def nested(tmp_value: str, other_body: str) -> str:
    text = NESTED_SEED.replace("pub fn value() -> u8 {\n    1\n}", f"pub fn value() -> u8 {{\n    {tmp_value}\n}}")
    return (text.replace("pub fn other() -> u8 {\n    1\n}", f"pub fn other() -> u8 {{\n    {tmp_value}\n}}")
            .replace(OTHER_BODY, other_body))


@pytest.fixture(scope="module")
def target(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("holes-target")


def verdict(tmp: Path, target: Path, seed: dict[str, str], candidate: dict[str, str], granted=frozenset()):
    """``(plain cargo test passes, integrity ok, integrity output)`` for ``candidate`` over ``seed``."""
    cargo = real_cargo(target)
    stage = integrity(tmp, seed, cargo)
    tree = make_tree(tmp, "cand", candidate)
    plain_ok, plain = cargo(tree, ["test", "--lib", "--bins"])
    ok, output = stage.check(tree, granted)
    return plain_ok, plain, ok, output


# --- finding 1 (blocker): an attribute stacked above a seed test region, and a fake test of the same name ---


@real_cargo_needed
@pytest.mark.parametrize("stacked", ["#[cfg(any())]", "#[cfg_attr(all(), cfg(any()))]"])
def test_a_stacked_cfg_and_a_fake_test_module_fail_integrity(tmp_path: Path, target: Path, stacked: str) -> None:
    candidate = TWO_FN + FAKE_TESTS + f"\n{stacked}\n" + TEST_MOD

    plain_ok, plain, ok, output = verdict(tmp_path, target, {"src/lib.rs": SEED_LIB}, {"src/lib.rs": candidate})

    assert plain_ok, plain
    assert not ok, "the fake module satisfied the seed's test name while value() changed"


@real_cargo_needed
def test_a_fake_test_in_the_bin_cannot_stand_in_for_a_lib_test(tmp_path: Path, target: Path) -> None:
    lib = TWO_FN + "#[cfg(any())]\n" + TEST_MOD
    main = "fn main() {}\n\n" + FAKE_TESTS

    plain_ok, plain, ok, _output = verdict(tmp_path, target, {"src/lib.rs": SEED_LIB},
                                           {"src/lib.rs": lib, "src/main.rs": main})

    assert plain_ok, plain
    assert not ok


@real_cargo_needed
def test_a_cfg_attr_path_on_the_seed_test_module_fails_integrity(tmp_path: Path, target: Path) -> None:
    lib = PATH_SEED["src/lib.rs"].replace("1\n}", "2\n}").replace(
        "#[cfg(test)]", '#[cfg_attr(all(), path = "fake.rs")]\n#[cfg(test)]')
    candidate = {"src/lib.rs": lib, "src/tests.rs": PATH_SEED["src/tests.rs"],
                 "src/fake.rs": "#[test]\nfn value_is_one() {}\n"}

    plain_ok, plain, ok, _output = verdict(tmp_path, target, PATH_SEED, candidate)

    assert plain_ok, plain
    assert not ok


@real_cargo_needed
def test_the_honest_break_still_fails_integrity_and_names_the_seed_test(tmp_path: Path, target: Path) -> None:
    plain_ok, plain, ok, output = verdict(tmp_path, target, {"src/lib.rs": SEED_LIB}, {"src/lib.rs": TWO})

    assert plain_ok is False or "value_is_one" in plain
    assert not ok and VALUE_IS_ONE in output


def test_a_stacked_attribute_gives_the_region_another_key_and_starts_it_at_the_attribute() -> None:
    plain = "fn a() {}\n\n#[cfg(test)]\nmod tests {}\n"
    stacked = "fn a() {}\n\n#[cfg(any())]\n#[cfg(test)]\nmod tests {}\n"
    (_s, _e, plain_key, _i), (start, _end, stacked_key, _inner) = (rust.cfg_test_regions(plain)[0],
                                                                   rust.cfg_test_regions(stacked)[0])

    assert plain_key != stacked_key and "cfg(any())" in stacked_key
    assert stacked[start:].startswith("#[cfg(any())]")


def test_swapping_a_candidate_with_a_stacked_attribute_keeps_the_seeds_module_and_drops_the_attribute() -> None:
    candidate = TWO_FN + FAKE_TESTS + "\n#[cfg(any())]\n" + TEST_MOD

    text = _swap_regions(candidate, SEED_LIB)

    assert "cfg(any())" not in text and text.count("fn value_is_one") == 2
    assert "#[cfg(test)]\nmod tests" in text


def test_the_cfg_lines_of_a_test_region_head_name_every_attribute_but_plain_cfg_test() -> None:
    stacked = '#[cfg_attr(all(), path = "x.rs")]\n#[cfg(any())]\n#[cfg(test)]\nmod tests;\n'

    assert rust.test_region_cfg_lines(stacked) == [1, 2]
    assert rust.test_region_cfg_lines("#[cfg(test)]\nmod tests {}\n") == []
    assert rust.test_region_cfg_lines("#[derive(Debug)]\n#[cfg(test)]\nmod tests {}\n") == []


def test_the_materializer_refuses_an_added_cfg_attribute_on_a_test_item(tmp_path: Path) -> None:
    for stacked in ("#[cfg(any())]", "#[cfg_attr(all(), cfg(any()))]"):
        candidate = SEED_LIB.replace("#[cfg(test)]\nmod", f"{stacked}\n#[cfg(test)]\nmod")
        assert "cfg or cfg_attr attribute" in (refusal(tmp_path / f"s{len(stacked)}", {"src/lib.rs": SEED_LIB},
                                                       {"src/lib.rs": candidate}) or "")


# --- finding 1(d) and 3: a same-named test outside the seed's region, or defined twice ---


def refusal(tmp: Path, seed: dict[str, str], candidate: dict[str, str], scope=frozenset()) -> str | None:
    """The materializer's refusal of ``candidate`` over ``seed`` at the harness and test-cfg checks, or None."""
    tmp.mkdir(parents=True, exist_ok=True)
    repo, sha = make_seed(tmp, seed)
    for path, text in candidate.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git("add", "-A", cwd=repo)
    diff = git("diff", "--cached", "-U0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", sha, "--", "src",
               cwd=repo)
    materializer = Materializer.__new__(Materializer)
    materializer.repo, materializer.seed_sha, materializer.test_texts = repo, sha, {}
    materializer.forbidden = [term for term in HARNESS_TERMS if term]
    try:
        materializer._refuse_harness_reach(repo, diff)
        materializer._refuse_cfg_test(repo, diff, scope, None)
    except ApplyRejected as rejected:
        return str(rejected)
    return None


def test_a_test_name_defined_twice_in_one_file_is_refused(tmp_path: Path) -> None:
    candidate = SEED_LIB.replace("#[cfg(test)]\nmod", FAKE_TESTS + "\n#[cfg(test)]\nmod")

    message = refusal(tmp_path, {"src/lib.rs": SEED_LIB}, {"src/lib.rs": candidate})

    assert message is not None and "value_is_one" in message and "2 times" in message


def test_a_unique_added_test_is_still_accepted(tmp_path: Path) -> None:
    added = SEED_LIB.replace("        assert_eq!(value(), 1);\n    }\n",
                             "        assert_eq!(value(), 1);\n    }\n\n    #[test]\n    fn value_is_positive() {\n"
                             "        assert!(value() > 0);\n    }\n")

    assert refusal(tmp_path, {"src/lib.rs": SEED_LIB}, {"src/lib.rs": added}) is None


# --- finding 2: a granted fn body that defines an item ---


@real_cargo_needed
def test_a_granted_test_with_a_nested_impl_fails_integrity(tmp_path: Path, target: Path) -> None:
    body = "    fn other_is_one() {\n" + NESTED_IMPL + "        assert_eq!(other(), 2);\n    }"

    plain_ok, plain, ok, output = verdict(tmp_path, target, {"src/lib.rs": NESTED_SEED},
                                          {"src/lib.rs": nested("2", body)}, frozenset({OTHER_IS_ONE}))

    assert plain_ok, plain
    assert not ok and "impl" in output


@real_cargo_needed
def test_a_granted_test_without_the_nested_impl_still_fails_on_the_seed_test_it_breaks(tmp_path: Path,
                                                                                      target: Path) -> None:
    body = "    fn other_is_one() {\n        assert_eq!(other(), 2);\n    }"

    _plain_ok, _plain, ok, output = verdict(tmp_path, target, {"src/lib.rs": NESTED_SEED},
                                            {"src/lib.rs": nested("2", body)}, frozenset({OTHER_IS_ONE}))

    assert not ok and "value_is_valid" in output


@pytest.mark.parametrize("item", ["impl Foo {}", "trait T {}", "macro_rules! m { () => {}; }", "mod inner {}",
                                  "use std::fmt;", "struct S;", "enum E { A }", "fn helper() {}", "static S: u8 = 1;",
                                  "const C: u8 = 1;", "type A = u8;", "union U { a: u8 }", "extern crate core;",
                                  "#[allow(dead_code)] pub(crate) fn helper() {}"])
def test_a_fn_body_item_is_found_at_statement_position(item: str) -> None:
    text = f"#[test]\nfn a() {{\n    {item}\n    assert_eq!(1, 1);\n}}"

    assert rust.body_items(text) is not None


def test_closures_and_let_bindings_are_not_body_items() -> None:
    text = "#[test]\nfn a() {\n    let f = |x: u8| x + 1;\n    let c = 1;\n    assert_eq!(f(c), 2);\n}"

    assert rust.body_items(text) is None
    assert rust.test_weakening(text, text.replace("2)", "2) ")) is None


def test_the_materializer_refuses_a_granted_test_that_gains_a_nested_impl(tmp_path: Path) -> None:
    body = "    fn other_is_one() {\n" + NESTED_IMPL + "        assert_eq!(other(), 2);\n    }"

    message = refusal(tmp_path, {"src/lib.rs": NESTED_SEED}, {"src/lib.rs": nested("2", body)},
                      frozenset({OTHER_IS_ONE}))

    assert message is not None and "defines an item" in message


# --- finding 4: cfg_attr(.., path = ..) ---


def test_a_cfg_attr_path_is_found_like_a_plain_path_attribute() -> None:
    text = 'fn a() {}\n#[cfg_attr(all(), path = "fake.rs")]\nmod tests;\n#[path = "x.rs"]\nmod y;\n'

    assert rust.path_attributes(text) == [2, 4]
    assert rust.path_attributes("#[cfg_attr(test, derive(Debug))]\nstruct S;\n") == []


def test_the_materializer_refuses_an_added_cfg_attr_path(tmp_path: Path) -> None:
    lib = PATH_SEED["src/lib.rs"].replace("#[cfg(test)]", '#[cfg_attr(all(), path = "fake.rs")]\n#[cfg(test)]')
    candidate = {"src/lib.rs": lib, "src/fake.rs": "#[test]\nfn value_is_one() {}\n"}

    assert refusal(tmp_path, PATH_SEED, candidate) is not None


# --- finding 5: production code that detects the test harness at run time ---


@pytest.mark.parametrize("term", ["current_exe", "/deps/", "RUST_TEST_", "--test-threads"])
def test_added_code_that_probes_the_test_harness_is_refused(tmp_path: Path, term: str) -> None:
    probe = f'pub fn harness() -> bool {{\n    format!("{{}}", "{term}").is_empty()\n}}\n'

    message = refusal(tmp_path, {"src/lib.rs": SEED_LIB}, {"src/lib.rs": SEED_LIB + "\n" + probe})

    assert message is not None and term in message


def test_the_review_note_names_the_residual_runtime_probe_risk() -> None:
    from evolve.finish import TEST_REVIEW_NOTE

    assert "at run time" in TEST_REVIEW_NOTE and "current_exe" in TEST_REVIEW_NOTE


# --- finding 1(c): listing per cargo target ---


METADATA = json.dumps({"packages": [{"targets": [{"kind": ["lib"], "name": "fixture", "test": True},
                                                 {"kind": ["bin"], "name": "fixture", "test": True}]}]})


class TargetStub:
    """A cargo stub that lists a different set of test names for the lib and the bin, in the seed and the candidate."""

    def __init__(self, seed: dict[str, list[str]], candidate: dict[str, list[str]]) -> None:
        self.seed, self.candidate = seed, candidate

    def __call__(self, tree: Path, args: list[str]) -> tuple[bool, str]:
        if args[0] == "metadata":
            return True, METADATA
        if "--list" not in args:
            return True, ""
        names = self.seed if tree.name == "integrity-seed" else self.candidate
        listed = names["lib"] + names["bin"] if "--bins" in args else names["lib"] if "--lib" in args else names["bin"]
        return True, "".join(f"{name}: test\n" for name in listed)


def test_a_seed_lib_test_is_not_satisfied_by_a_bin_test_of_the_same_name(tmp_path: Path) -> None:
    stub = TargetStub({"lib": [VALUE_IS_ONE], "bin": []}, {"lib": [], "bin": [VALUE_IS_ONE]})
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, stub)

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO}))

    assert not ok and VALUE_IS_ONE in output and "missing" in output


def test_a_seed_test_listed_in_its_own_target_passes_the_listing(tmp_path: Path) -> None:
    stub = TargetStub({"lib": [VALUE_IS_ONE], "bin": []}, {"lib": [VALUE_IS_ONE], "bin": ["extra"]})
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, stub)

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO}))

    assert ok, output


# --- finding 6: an infrastructure fault is no verdict ---


class ListFails:
    def __init__(self, output: str) -> None:
        self.output = output

    def __call__(self, tree: Path, args: list[str]) -> tuple[bool, str]:
        if args[0] == "metadata":
            return True, METADATA
        return False, self.output


def test_a_cargo_failure_that_is_no_compile_error_is_an_infrastructure_failure(tmp_path: Path) -> None:
    from evolve.integrity import InfrastructureFailure

    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, ListFails("error: linker `cc` not found"))

    with pytest.raises(InfrastructureFailure, match="linker"):
        stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO}))


def test_a_candidate_compile_error_is_still_the_candidates_failure(tmp_path: Path) -> None:
    seed_ok = TargetStub({"lib": [VALUE_IS_ONE], "bin": []}, {"lib": [], "bin": []})
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, seed_ok)
    stage.seed_names()
    stage.cargo = ListFails("error: could not compile `fixture` (lib test)")

    ok, output = stage.check(make_tree(tmp_path, "cand", {"src/lib.rs": TWO}))

    assert not ok and "do not compile" in output


def test_a_seed_that_does_not_compile_raises_a_runtime_error(tmp_path: Path) -> None:
    stage = integrity(tmp_path, {"src/lib.rs": SEED_LIB}, ListFails("error: could not compile `fixture`"))

    with pytest.raises(RuntimeError, match="cannot list the seed's tests"):
        stage.seed_names()


def test_a_timeout_in_the_default_cargo_is_an_infrastructure_failure(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    from evolve import cli
    from evolve.integrity import InfrastructureFailure

    class Hangs:
        pid = 0
        returncode = 0

        def __init__(self, argv, **kwargs) -> None:
            pass

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("cargo", timeout)
            return "", ""

    monkeypatch.setattr(cli.subprocess, "Popen", Hangs)
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(cli.run, "RESULTS_DIR", tmp_path / "results")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(InfrastructureFailure, match="timed out"):
        cli.default_cargo(worktree, ["test"])
    assert cli.default_just_check(worktree)[0] is False


def test_an_infrastructure_fault_is_worded_by_cause_and_is_not_memoized(two_tests) -> None:
    from evolve.integrity import INFRASTRUCTURE_HEADLINE, InfrastructureFailure

    run = Repair(two_tests, [])
    calls: list[frozenset] = []

    def check(worktree, granted=frozenset()):
        calls.append(granted)
        if len(calls) == 1:
            raise InfrastructureFailure("cargo timed out")
        return True, ""

    run.evo.integrity = SimpleNamespace(check=check)
    result = SimpleNamespace(candidate={SRC: "a patch"})

    first = run.evo._integrity(result, two_tests.tmp)
    second = run.evo._integrity(result, two_tests.tmp)

    assert first[0] is False and first[1].startswith(INFRASTRUCTURE_HEADLINE) and "cargo timed out" in first[1]
    assert "Existing seed tests failed" not in first[1]
    assert second == (True, "") and len(calls) == 2


# --- finding 7 ---


def test_a_seed_names_error_still_releases_the_candidate_worktree(monkeypatch: pytest.MonkeyPatch, two_tests) -> None:
    run = Repair(two_tests, [])
    run.evo.just_check = lambda worktree: (True, "just check: ok")
    released: list[str] = []
    monkeypatch.setattr(run.evo.materializer, "release", lambda sha: released.append(sha))

    def check(worktree, granted=frozenset()):
        raise RuntimeError("cannot list the seed's tests")

    run.evo.integrity = SimpleNamespace(check=check)
    child = {**seed_candidate(), SRC: patch_for(two_tests, {"src/lib.rs": FORMATTED.replace("    2\n", "    3\n")})}

    with pytest.raises(RuntimeError, match="cannot list"):
        run.evo.precheck(child)

    assert len(released) == 1


def test_a_custom_just_check_keeps_the_integrity_stage_when_cargo_is_wired(world) -> None:
    assert build(world, cargo=lambda tree, args: (True, "")).integrity is not None
    assert build(world).integrity is None


def test_the_integrity_stage_off_warning_reaches_stderr(monkeypatch: pytest.MonkeyPatch, capsys, world) -> None:
    from evolve import loop
    from evolve_support import main

    monkeypatch.setattr(loop.Evolution, "run", lambda self: 0)

    assert main(world) == 0
    assert "integrity stage is OFF" in capsys.readouterr().err
    assert main(world, cargo=lambda tree, args: (True, "")) == 0
    assert "integrity stage is OFF" not in capsys.readouterr().err


def test_default_just_check_touches_the_sources_before_it_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from evolve import cli

    class Quick:
        returncode = 0

        def __init__(self, argv, **kwargs) -> None:
            pass

        def communicate(self, timeout=None):
            return "ok", ""

    monkeypatch.setattr(cli.subprocess, "Popen", Quick)
    monkeypatch.setattr(cli.run, "RESULTS_DIR", tmp_path / "results")
    worktree = tmp_path / "wt"
    source = worktree / "src" / "lib.rs"
    source.parent.mkdir(parents=True)
    source.write_text("")
    os.utime(source, (1000, 1000))

    assert cli.default_just_check(worktree) == (True, "ok")

    assert source.stat().st_mtime > 1000
