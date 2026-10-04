"""Adversarial checks on the evolve loop's boundaries: the cfg(test) detector, stop precedence, and the proposer diff."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import candidate as candidates
from evolve import rust
from evolve.calls import StopState
from evolve.materialize import Materializer
from evolve_support import copy_tilth, git, make_world, result_stream, seed_candidate

RAW = 'r#"a { brace } in a raw string"#'


def test_cfg_test_spans_ignore_braces_in_literals_and_comments() -> None:
    text = "\n".join([
        "fn live() {}",                                   # 1
        "#[cfg(test)]",                                   # 2
        "mod tests {",                                    # 3
        f"    const RAW: &str = {RAW};",                  # 4
        "    const OPEN: char = '{';",                    # 5
        "    /* nested /* { */ still a comment } */",     # 6
        "    // } a closing brace in a line comment",     # 7
        "    fn borrow<'a>(text: &'a str) -> &'a str { text }",  # 8
        '    const ESCAPED: &str = "quote \\" then }";',  # 9
        "}",                                              # 10
        "fn after() {}",                                  # 11
        "#[cfg(test)]",                                   # 12
        'const SPAN: &str = "ends; with } chars";',       # 13
        "fn tail() {}",                                   # 14
    ])
    assert rust.cfg_test_spans(text) == [(2, 10), (12, 13)]


def _patch(repo: Path, seed: str, path: str, old: str, new: str) -> str:
    scratch = repo.parent / f"press-scratch-{abs(hash((path, old, new)))}"
    git("worktree", "add", "-q", "--detach", str(scratch), seed, cwd=repo)
    text = (scratch / path).read_text()
    assert old in text
    (scratch / path).write_text(text.replace(old, new, 1))
    diff = git("diff", seed, cwd=scratch)
    git("worktree", "remove", "--force", str(scratch), cwd=repo)
    return diff


@pytest.fixture(scope="module")
def tilth(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    return copy_tilth(tmp_path_factory.mktemp("press-tilth"))


def test_live_code_beside_cfg_test_items_is_editable(tilth, tmp_path: Path) -> None:
    repo, seed = tilth
    base = candidates.read_seed(repo, seed)
    materializer = Materializer(repo, seed, "press-live", tmp_path / "work")
    live = _patch(repo, seed, "src/mcp/mod.rs", "    SERVER_INSTRUCTIONS.trim_end().to_string()",
                  "    SERVER_INSTRUCTIONS.trim_end().to_owned()")
    guarded = _patch(repo, seed, "src/mcp/mod.rs", 'const CWD_PATHS_SPAN: &str = "DO NOT omit',
                     'const CWD_PATHS_SPAN: &str = "NEVER omit')

    sha = materializer.materialize({**base, "src_patch": live})
    assert "trim_end().to_owned()" in git("show", f"{sha}:src/mcp/mod.rs", cwd=repo)
    with pytest.raises(Exception, match=r"cfg\(test\)"):
        materializer.materialize({**base, "src_patch": guarded})


def test_byte_lock_literal_edits_are_rewritten_not_refused(tilth, tmp_path: Path) -> None:
    repo, seed = tilth
    base = candidates.read_seed(repo, seed)
    materializer = Materializer(repo, seed, "press-lock", tmp_path / "work")
    patch = _patch(repo, seed, "src/mcp/mod.rs", 'ends_with("DO NOT re-read expanded search content.")',
                   'ends_with("a proposer guessed this literal")')
    new_mcp = base["prompts/mcp.md"] + "\nA new last line."

    sha = materializer.materialize({**base, "prompts/mcp.md": new_mcp, "src_patch": patch})
    mod = git("show", f"{sha}:src/mcp/mod.rs", cwd=repo)
    assert 'ends_with("A new last line.")' in mod
    assert "a proposer guessed this literal" not in mod
    assert f"{len(new_mcp.encode())}," in mod


def test_quota_outranks_a_later_ceiling() -> None:
    stop = StopState()
    seen: list[str] = []
    stop.listeners.append(seen.append)
    stop.set("ceiling")
    stop.set("quota")
    stop.set("ceiling")
    stop.set("plateau")
    assert stop.reason == "quota"
    assert seen == ["ceiling", "quota"]
    with pytest.raises(ValueError):
        stop.set("budget")


def test_proposer_diff_carries_new_and_deleted_files_and_applies(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)

    def spawn(argv, **kwargs):
        export = Path(kwargs["cwd"])
        (export / "src" / "fresh.rs").write_text("pub fn fresh() {}\n")
        (export / "src" / "main.rs").unlink()
        (export / "Cargo.toml").write_text("[package]\nname = \"tilth\"\nversion = \"9.9.9\"\n")
        return subprocess.CompletedProcess(argv, 0, result_stream("done", cost=0.1), "")

    from evolve_support import build
    evo = build(world, spawn=spawn)
    assert evo.preflight() is None
    diff = evo.proposer.propose_src_patch(evo.seed, [])

    assert "+++ b/src/fresh.rs" in diff and "--- a/src/main.rs" in diff
    assert "Cargo.toml" not in diff
    sha = evo.materializer.materialize({**seed_candidate(), "src_patch": diff})
    tree = git("ls-tree", "-r", "--name-only", sha, cwd=world.repo).split()
    assert "src/fresh.rs" in tree and "src/main.rs" not in tree
    assert git("show", f"{sha}:Cargo.toml", cwd=world.repo) == git("show", f"{world.seed_sha}:Cargo.toml",
                                                                   cwd=world.repo)


def test_failed_proposer_call_keeps_parent_and_charges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = make_world(monkeypatch, tmp_path)
    failure = '{"type": "result", "subtype": "error_during_execution", "is_error": true, "result": "boom", ' \
              '"total_cost_usd": 0.07}\n'

    def spawn(argv, **kwargs):
        (Path(kwargs["cwd"]) / "src" / "lib.rs").write_text("pub fn half_done() {}\n")
        return subprocess.CompletedProcess(argv, 1, failure, "")

    from evolve_support import build
    evo = build(world, spawn=spawn)
    assert evo.preflight() is None
    spent = evo.ledger.spent
    parent = {**seed_candidate(), "src_patch": ""}

    assert evo.dispatcher(parent, {"src_patch": [{"records": []}]}, ["src_patch"]) == {"src_patch": ""}
    assert evo.ledger.spent == pytest.approx(spent + 0.07)
    assert evo.stop.reason is None


def test_content_id_hashes_utf8_not_ascii_escapes() -> None:
    candidate = {**seed_candidate(), "prompts/mcp.md": "tilth — code intelligence"}
    import hashlib
    import json
    escaped = hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert candidates.content_id(candidate) != escaped
    assert candidates.content_id(candidate) == hashlib.sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def test_dispatcher_sends_each_record_once() -> None:
    from evolve import engine
    seen: list[list[dict]] = []
    dispatcher = engine.Dispatcher(StopState(), reflect=lambda name, text, records: seen.append(records) or text,
                                   propose=lambda candidate, records: seen.append(records) or "")
    first, second = {"row": {"task": "dev_a", "repetition": 1}}, {"row": {"task": "dev_b", "repetition": 1}}
    entry_a, entry_b = {"records": [first]}, {"records": [second]}
    dispatcher(seed_candidate(), {"prompts/mcp.md": [entry_a, entry_b, entry_a],
                                  "src_patch": [entry_b, entry_b]}, ["prompts/mcp.md", "src_patch"])
    assert seen == [[first, second], [second]]


def test_run_removes_its_candidate_worktrees(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from evolve import engine
    from evolve_support import build, child, scripted_engine
    world = make_world(monkeypatch, tmp_path)
    fake, _seen = scripted_engine([child(seed_candidate(), dev_a="1", dev_b="1", cheap_a="1", tag="c")])
    monkeypatch.setattr(engine, "optimize_anything", fake)
    other = tmp_path / "unrelated-worktree"
    git("worktree", "add", "-q", "--detach", str(other), world.seed_sha, cwd=world.repo)
    evo = build(world)

    assert evo.run() == 0
    listed = git("worktree", "list", "--porcelain", cwd=world.repo)
    assert "evolve" not in listed and str(other) in listed
    refs = git("for-each-ref", "--format=%(objectname)", "refs/evolve/run1", cwd=world.repo).split()
    assert len(refs) == 2 and all(git("cat-file", "-t", sha, cwd=world.repo).strip() == "commit" for sha in refs)
