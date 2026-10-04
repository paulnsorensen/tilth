"""The applier: one commit per candidate content id, on top of the seed commit."""

import difflib
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from . import rust
from .candidate import SRC_PATCH, TEXT_COMPONENTS, content_id, read_seed, ref_name
from .gitops import GitError, git

ALLOWED_PREFIXES = ("src/", "prompts/")
# Names that point a candidate's code at the harness; the loop adds the panel file and the data directory.
HARNESS_TERMS = ("benchmark", ".cheese", "tilth_bench", "TILTH_BENCH_DATA")
BYTE_LOCK_FILE = "src/mcp/mod.rs"
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ApplyRejected(RuntimeError):
    """The candidate cannot become a commit; ``tail`` is the failing output for side info."""

    def __init__(self, reason: str, tail: str = "") -> None:
        super().__init__(reason)
        self.tail = f"{reason}\n{tail}".strip()


def changed_lines(diff: str) -> dict[str, tuple[set[int], set[int]]]:
    """Map each file in a ``-U0`` diff to its (removed seed lines, added candidate lines)."""
    files: dict[str, tuple[set[int], set[int]]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[1]
            files[current] = (set(), set())
        elif current and (hunk := _HUNK.match(line)):
            old, old_count = int(hunk[1]), int(hunk[2] or 1)
            new, new_count = int(hunk[3]), int(hunk[4] or 1)
            files[current][0].update(range(old, old + old_count))
            files[current][1].update(range(new, new + new_count))
    return files


def _line_changes(old: str, new: str) -> tuple[set[int], set[int]]:
    """(removed ``old`` lines, added ``new`` lines), 1-based, of a line diff between two texts."""
    removed: set[int] = set()
    added: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag != "equal":
            removed.update(range(old_start + 1, old_end + 1))
            added.update(range(new_start + 1, new_end + 1))
    return removed, added


class Materializer:
    """``materialize(candidate) -> sha``, memoized by content id.

    The first call for a content id writes the text components into a worktree at
    the seed, applies the cumulative ``src_patch``, refuses changes outside
    ``src/**`` and ``prompts/**``, inside ``#[cfg(test)]`` code, or reaching for the
    harness (a harness name, or an ``include*!`` outside ``src/`` and ``prompts/``), regenerates
    ``AGENTS.md``, rewrites the byte-lock literals, commits with the seed as
    parent, and points ``evolve/<run-id>/<content-id>`` at the commit.
    """

    def __init__(self, repo: Path, seed_sha: str, run_id: str, work_dir: Path, *,
                 forbidden: Iterable[str] = ()) -> None:
        self.repo = Path(repo)
        self.forbidden = [term for term in dict.fromkeys((*HARNESS_TERMS, *forbidden)) if term]
        self.seed_sha = seed_sha
        self.run_id = run_id
        self.work_dir = Path(work_dir)
        self.seed = read_seed(self.repo, seed_sha)
        self._seed_id = content_id(self.seed)
        self._memo: dict[str, str | ApplyRejected] = {}
        self._worktrees: dict[str, Path] = {}

    def materialize(self, candidate: Mapping[str, str]) -> str:
        cid = content_id(candidate)
        memo = self._memo.get(cid)
        if isinstance(memo, ApplyRejected):
            raise memo
        if memo is not None:
            return memo
        try:
            sha = self.seed_sha if cid == self._seed_id else self._commit(candidate, cid)
        except ApplyRejected as rejected:
            self._memo[cid] = rejected
            raise
        git("update-ref", ref_name(self.run_id, candidate), sha, cwd=self.repo)
        self._memo[cid] = sha
        return sha

    def worktree(self, sha: str) -> Path:
        """The checkout a candidate commit is checked and built in."""
        if sha not in self._worktrees:
            self._worktrees[sha] = self._add_worktree(self.work_dir / sha[:12], sha)
        return self._worktrees[sha]

    def cleanup(self) -> None:
        """Remove every worktree this materializer created; the ``refs/evolve`` refs keep the commits."""
        for path in self._worktrees.values():
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo, capture_output=True)
            shutil.rmtree(path, ignore_errors=True)
        self._worktrees.clear()
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)

    def _add_worktree(self, path: Path, sha: str) -> Path:
        if path.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo, capture_output=True)
            shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(path), sha, cwd=self.repo)
        return path

    def _commit(self, candidate: Mapping[str, str], cid: str) -> str:
        worktree = self._add_worktree(self.work_dir / f"candidate-{cid[:12]}", self.seed_sha)
        try:
            sha = self._build_commit(worktree, candidate, cid)
        except BaseException:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, capture_output=True)
            raise
        self._worktrees[sha] = worktree
        return sha

    def _build_commit(self, worktree: Path, candidate: Mapping[str, str], cid: str) -> str:
        for name in TEXT_COMPONENTS:
            (worktree / name).write_bytes(candidate[name].encode("utf-8"))
        patch = candidate[SRC_PATCH]
        if patch.strip():
            try:
                git("apply", "--whitespace=nowarn", "-", cwd=worktree, input=patch)
            except GitError as error:
                raise ApplyRejected("the cumulative src_patch does not apply onto the seed", error.output) from error
        git("add", "-A", cwd=worktree)
        changed = git("diff", "--cached", "--name-only", "--no-renames", self.seed_sha, cwd=worktree).split("\n")
        changed = [path for path in changed if path]
        outside = [path for path in changed if not path.startswith(ALLOWED_PREFIXES)]
        if outside:
            raise ApplyRejected(f"changes outside src/** and prompts/**: {', '.join(outside)}")
        diff = git("diff", "--cached", "-U0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/",
                   self.seed_sha, "--", "src", cwd=worktree)
        self._refuse_harness_reach(worktree, diff)
        self._refuse_cfg_test(worktree, diff)
        regen = subprocess.run(["bash", "scripts/regen-agents-md.sh"], cwd=worktree, capture_output=True, text=True)
        if regen.returncode != 0:
            raise ApplyRejected("scripts/regen-agents-md.sh failed", regen.stdout + regen.stderr)
        mod = worktree / BYTE_LOCK_FILE
        if mod.is_file():
            try:
                mod.write_text(rust.rewrite_byte_lock(mod.read_text(encoding="utf-8"), candidate["prompts/mcp.md"]),
                               encoding="utf-8")
            except ValueError as error:
                raise ApplyRejected(f"{BYTE_LOCK_FILE}: {error}") from error
        git("add", "-A", cwd=worktree)
        git("commit", "-q", "--allow-empty", "--no-verify", "-m", f"evolve {self.run_id}: candidate {cid[:12]}",
            cwd=worktree)
        return git("rev-parse", "HEAD", cwd=worktree).strip()

    def _seed_text(self, path: str) -> str | None:
        try:
            return git("show", f"{self.seed_sha}:{path}", cwd=self.repo)
        except GitError:
            return None

    def _refuse_harness_reach(self, worktree: Path, diff: str) -> None:
        """Refuse added ``src/**`` lines that name the harness or include a file outside ``src/`` and ``prompts/``."""
        root = Path(worktree).resolve()
        allowed = [root / prefix.rstrip("/") for prefix in ALLOWED_PREFIXES]
        for path, (_removed, added) in changed_lines(diff).items():
            new_file = worktree / path
            if not added or not new_file.is_file():
                continue
            text = new_file.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            for number in sorted(added):
                line = lines[number - 1].lower() if number <= len(lines) else ""
                if any(term.lower() in line for term in self.forbidden):
                    # The tail reaches the proposer, so it does not repeat the harness name.
                    raise ApplyRejected(f"{path}:{number} names the benchmark harness")
            if not path.endswith(".rs"):
                continue
            for first, last, macro, argument in rust.include_invocations(text):
                if not any(first <= number <= last for number in added):
                    continue
                where = f"{path}:{first} {macro}!"
                if "concat!" in argument or "env!" in argument:
                    raise ApplyRejected(f"{where} builds its path with concat! or env!")
                value = rust.string_literal(argument)
                if value is None:
                    raise ApplyRejected(f"{where} takes {argument!r}, not a string literal")
                target = Path(os.path.realpath(new_file.parent / value))
                if not any(target.is_relative_to(prefix) for prefix in allowed):
                    raise ApplyRejected(f"{where} reaches {value}, outside src/** and prompts/**")

    def _refuse_cfg_test(self, worktree: Path, diff: str) -> None:
        """Refuse any changed line inside a ``#[cfg(test)]`` item or in a ``#[cfg(test)]`` module file.

        The three byte-lock literals are exempt, and nothing else on their lines: the applier rewrites them itself.
        """
        for path, (removed, added) in changed_lines(diff).items():
            if not path.endswith(".rs"):
                continue
            new_file = worktree / path
            old_text = self._seed_text(path) or ""
            new_text = new_file.read_text(encoding="utf-8") if new_file.is_file() else ""
            for declarer in rust.declaring_files(path):
                declared_new = worktree / declarer
                texts = [self._seed_text(declarer) or "",
                         declared_new.read_text(encoding="utf-8") if declared_new.is_file() else ""]
                if any(path in rust.test_module_files(declarer, text) for text in texts):
                    raise ApplyRejected(f"{path} is a #[cfg(test)] module file; candidates may not change it")
            if path == BYTE_LOCK_FILE:
                # Only the three literals the applier rewrites may differ; the rest of their lines may not.
                old_text, new_text = rust.blank_byte_lock(old_text), rust.blank_byte_lock(new_text)
                removed, added = _line_changes(old_text, new_text)
            for lines, text in ((removed, old_text), (added, new_text)):
                for first, last in rust.cfg_test_spans(text):
                    touched = sorted(line for line in lines if first <= line <= last)
                    if touched:
                        raise ApplyRejected(f"{path}:{touched[0]} is inside a #[cfg(test)] item (lines {first}-{last})")
