"""The applier: one commit per candidate content id, on top of the seed commit."""

import difflib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from . import rust
from .candidate import MAX_BYTES, SRC_PATCH, TEXT_COMPONENTS, content_id, read_seed, ref_name
from .gitops import GitError, git
from .rustfmt import format_tree

ALLOWED_PREFIXES = ("src/", "prompts/")
# The candidate `just check` sets this to 1, and the Rust tests then skip their prompt phrase pins.
PHRASE_PIN_EXEMPT_ENV = "TILTH_EVOLVE_CANDIDATE"
# Names that point a candidate's code at the harness; the loop adds the panel file and the data directory.
# The exemption variable is one: candidate code that names it could keep the exemption on outside the search.
HARNESS_TERMS = ("benchmark", ".cheese", "tilth_bench", "tilth-bench", "TILTH_BENCH_DATA", PHRASE_PIN_EXEMPT_ENV)
BYTE_LOCK_FILE = "src/mcp/mod.rs"
REGULAR_MODES = {"000000", "100644", "100755"}
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ApplyRejected(RuntimeError):
    """The candidate cannot become a commit; ``tail`` is the failing output for side info."""

    def __init__(self, reason: str, tail: str = "") -> None:
        super().__init__(reason)
        self.tail = f"{reason}\n{tail}".strip()


def _diff_path(marker: str, line: str) -> str | None:
    """The path on a ``---`` or ``+++`` line; None for ``/dev/null``. Git appends a tab when the path has spaces."""
    name = line[4:].removesuffix("\t")
    if name == "/dev/null":
        return None
    if not name.startswith(marker):
        raise ApplyRejected(f"cannot read a changed path from the diff line {line!r}")
    return name[len(marker):]


def changed_lines(diff: str) -> dict[str, tuple[set[int], set[int]]]:
    """Map each file in a ``-U0`` diff to its (removed seed lines, added candidate lines).

    The path comes from the ``---`` and ``+++`` lines, so a path with spaces keeps its full name.
    """
    files: dict[str, tuple[set[int], set[int]]] = {}
    current = None
    old_path = None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current = old_path = None
        elif line.startswith("--- ") and current is None:
            old_path = _diff_path("a/", line)
        elif line.startswith("+++ ") and current is None:
            current = _diff_path("b/", line) or old_path
            if current is not None:
                files[current] = (set(), set())
        elif current and (hunk := _HUNK.match(line)):
            old, old_count = int(hunk[1]), int(hunk[2] or 1)
            new, new_count = int(hunk[3]), int(hunk[4] or 1)
            files[current][0].update(range(old, old + old_count))
            files[current][1].update(range(new, new + new_count))
    return files


def _read(path: Path) -> str:
    """File text with every byte kept, so line numbers count only ``\\n`` as git does."""
    return path.read_bytes().decode("utf-8", errors="replace")


def _line_changes(old: str, new: str) -> tuple[set[int], set[int]]:
    """(removed ``old`` lines, added ``new`` lines), 1-based, of a line diff between two texts."""
    removed: set[int] = set()
    added: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag != "equal":
            removed.update(range(old_start + 1, old_end + 1))
            added.update(range(new_start + 1, new_end + 1))
    return removed, added


def _cfg_test_code(path: str, text: str, module_file: bool) -> tuple[list[tuple[int, int]], dict[str, tuple[int, int]]]:
    """The test-code line spans of ``text`` (all of it for a ``#[cfg(test)]`` module file) and, by name, the
    ``#[test]`` fn spans inside them."""
    spans = [(1, text.count("\n") + 1)] if module_file else rust.cfg_test_spans(text)
    tests = {name: (first, last) for name, first, last in rust.test_functions(path, text)
             if any(low <= first and last <= high for low, high in spans)}
    return spans, tests


def _refuse_oversize(candidate: Mapping[str, str]) -> None:
    """Refuse a text over its byte cap before a worktree or ``just check`` is spent on it."""
    for name, limit in MAX_BYTES.items():
        size = len(candidate[name].encode("utf-8"))
        if size > limit:
            raise ApplyRejected(f"{name} is {size} bytes; its MCP field holds at most {limit} bytes")


class Materializer:
    """``materialize(candidate) -> sha``, memoized by content id.

    The first call for a content id takes these steps:

    1. Refuse a text over its ``MAX_BYTES`` cap. Then write the text components into a worktree at the seed.
    2. Apply the cumulative ``src_patch``, then run ``cargo fmt`` on the tree so the commit holds formatted code.
    3. Refuse changes outside ``src/**`` and ``prompts/**``, and changes inside ``#[cfg(test)]`` code. The one
       exception is a patch in ``test_scope``: a repaired child may change the ``#[test]`` fns it names, but may
       not disable or gut them. A patch in ``test_keep`` must leave its parent's granted tests byte-equal.
    4. Refuse reach for the harness: a harness name, an ``include*!`` outside ``src/`` and ``prompts/``,
       an alias of an include macro, or ``cfg!(test)``.
    5. Regenerate ``AGENTS.md`` and rewrite the byte-lock literals, then format again when the rewrite changed
       ``src/mcp/mod.rs``, since a long last prompt line pushes its assert past rustfmt's width.
    6. Commit with the seed as parent, and point ``evolve/<run-id>/<content-id>`` at the commit.
    """

    def __init__(self, repo: Path, seed_sha: str, run_id: str, work_dir: Path, *,
                 forbidden: Iterable[str] = (), fmt: Callable[[Path], object] | None = None) -> None:
        self.fmt = fmt or format_tree
        # ``src_patch`` text -> names of the failing tests (as cargo prints them) that patch may change. The
        # key is the patch, not the content id, so a child that only changes a text component keeps the grant.
        # The dispatcher fills it before the candidate is materialized.
        self.test_scope: dict[str, frozenset[str]] = {}
        # ``src_patch`` text -> (parent patch, test names) for a patch that extends a granted parent without a
        # repair. Those tests carry the parent's edits and must stay byte-equal to the parent's.
        self.test_keep: dict[str, tuple[str, frozenset[str]]] = {}
        # ``src_patch`` text -> {granted test name: its text as committed}, read to enforce ``test_keep``.
        self.test_texts: dict[str, dict[str, str]] = {}
        # Content id -> {test name: diff against the seed} for each committed candidate that changes tests.
        self.test_diffs: dict[str, dict[str, str]] = {}
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
            if cid == self._seed_id:
                sha = self.seed_sha
            else:
                _refuse_oversize(candidate)
                sha = self._commit(candidate, cid)
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

    def release(self, sha: str) -> None:
        """Remove the worktree of one candidate commit, as ``cleanup`` does for all of them."""
        path = self._worktrees.pop(sha, None)
        if path is not None:
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo, capture_output=True)
            shutil.rmtree(path, ignore_errors=True)

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
            # Before the checks, so they judge the code that gets committed. A parse error leaves the tree as is.
            self.fmt(worktree)
        git("add", "-A", cwd=worktree)
        raw = git("diff", "--cached", "--raw", "-z", "--no-renames", self.seed_sha, cwd=worktree).split("\0")
        changed = raw[1::2]
        # Only regular files may change: a symlink or gitlink under src/ can point outside the allowlist.
        special = [path for meta, path in zip(raw[0::2], changed) if meta.split()[1] not in REGULAR_MODES]
        if special:
            raise ApplyRejected(f"changes a symlink or submodule instead of a regular file: {', '.join(special)}")
        outside = [path for path in changed if not path.startswith(ALLOWED_PREFIXES)]
        if outside:
            raise ApplyRejected(f"changes outside src/** and prompts/**: {', '.join(outside)}")
        diff = git("diff", "--cached", "-U0", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/",
                   self.seed_sha, "--", "src", cwd=worktree)
        self._refuse_harness_reach(worktree, diff)
        changes, texts = self._refuse_cfg_test(worktree, diff, self.test_scope.get(patch, frozenset()),
                                               self.test_keep.get(patch))
        regen = subprocess.run(["bash", "scripts/regen-agents-md.sh"], cwd=worktree, capture_output=True, text=True)
        if regen.returncode != 0:
            raise ApplyRejected("scripts/regen-agents-md.sh failed", regen.stdout + regen.stderr)
        mod = worktree / BYTE_LOCK_FILE
        if mod.is_file():
            before = mod.read_text(encoding="utf-8")
            try:
                after = rust.rewrite_byte_lock(before, candidate["prompts/mcp.md"])
            except ValueError as error:
                raise ApplyRejected(f"{BYTE_LOCK_FILE}: {error}") from error
            mod.write_text(after, encoding="utf-8")
            if after != before:
                self.fmt(worktree)
        git("add", "-A", cwd=worktree)
        if changes:
            self.test_diffs[cid] = changes
        if texts:
            self.test_texts[patch] = texts
        stamp = f"@{git('log', '-1', '--format=%ct', self.seed_sha, cwd=worktree).strip()} +0000"
        git("commit", "-q", "--allow-empty", "--no-verify", "-m", f"evolve: candidate {cid[:12]}", cwd=worktree,
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        return git("rev-parse", "HEAD", cwd=worktree).strip()

    def _seed_text(self, path: str) -> str | None:
        try:
            return git("show", f"{self.seed_sha}:{path}", cwd=self.repo)
        except GitError:
            return None

    def _refuse_harness_reach(self, worktree: Path, diff: str) -> None:
        """Refuse added ``src/**`` lines that name the harness, include a file outside ``src/`` and ``prompts/``,
        import an include macro, test ``cfg!(test)``, or carry a ``#[path]`` attribute."""
        root = Path(worktree).resolve()
        allowed = [root / prefix.rstrip("/") for prefix in ALLOWED_PREFIXES]
        for path, (_removed, added) in changed_lines(diff).items():
            new_file = worktree / path
            if not added or not new_file.is_file():
                continue
            text = _read(new_file)
            lines = text.split("\n")
            for number in sorted(added):
                line = lines[number - 1].lower() if number <= len(lines) else ""
                if any(term.lower() in line for term in self.forbidden):
                    # The tail reaches the proposer, so it does not repeat the harness name.
                    raise ApplyRejected(f"{path}:{number} names the benchmark harness")
            # Every file under src/ is lexed, not only .rs: an include! can pull in any extension.
            for first, last in rust.include_aliases(text):
                if any(first <= number <= last for number in added):
                    raise ApplyRejected(f"{path}:{first} imports include, include_str, or include_bytes by a path, "
                                        "which can rename the macro and hide its file")
            for number in rust.bare_macro_names(text):
                if number in added:
                    raise ApplyRejected(f"{path}:{number} names include, include_str, include_bytes, or cfg without "
                                        "a call, which can pass the macro through a macro_rules! parameter")
            for first, last in rust.cfg_test_macros(text):
                if any(first <= number <= last for number in added):
                    raise ApplyRejected(f"{path}:{first} uses cfg!(..test..) or cfg!(..debug_assertions..), which makes code "
                                        "differ between the gate and the release build")
            for number in rust.path_attributes(text):
                if number in added:
                    raise ApplyRejected(f"{path}:{number} uses a #[path] attribute, which can point a module outside "
                                        "src/** and prompts/**")
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

    def _refuse_cfg_test(self, worktree: Path, diff: str, scope: frozenset[str] = frozenset(),
                         keep: tuple[str, frozenset[str]] | None = None) -> tuple[dict[str, str], dict[str, str]]:
        """Refuse any changed line inside a ``#[cfg(test)]`` item or in a ``#[cfg(test)]`` module file.

        The three byte-lock literals are exempt, and nothing else on their lines: the applier rewrites them itself.
        ``scope`` holds the names of failing tests, as cargo prints them. Lines inside those ``#[test]`` fns may
        change, and deleting one is refused. An edited one must stay a test: ``rust.test_weakening`` refuses a
        disabled or gutted one. ``keep`` is ``(parent patch, names)``: those tests carry the parent's edits, so
        their text must equal the parent's. Returns each changed test fn's name with its diff against the seed,
        and the text of every granted test.
        """
        changes: dict[str, str] = {}
        granted_texts: dict[str, str] = {}
        kept = keep[1] - scope if keep else frozenset()
        granted = scope | kept
        parent_texts = self.test_texts.get(keep[0]) if keep else None
        if keep and parent_texts is None:
            raise ApplyRejected("the parent candidate's granted tests are unknown; materialize the parent first")
        for path, (removed, added) in changed_lines(diff).items():
            if not path.endswith(".rs"):
                continue
            new_file = worktree / path
            old_text = self._seed_text(path) or ""
            new_text = _read(new_file) if new_file.is_file() else ""
            module_file = False
            for declarer in rust.declaring_files(path):
                declared_new = worktree / declarer
                texts = [self._seed_text(declarer) or "",
                         _read(declared_new) if declared_new.is_file() else ""]
                if any(path in rust.test_module_files(declarer, text) for text in texts):
                    if not granted:
                        raise ApplyRejected(f"{path} is a #[cfg(test)] module file; candidates may not change it")
                    module_file = True
            if path == BYTE_LOCK_FILE:
                # Only the three literals the applier rewrites may differ; the rest of their lines may not.
                old_text, new_text = rust.blank_byte_lock(old_text), rust.blank_byte_lock(new_text)
                removed, added = _line_changes(old_text, new_text)
            old_spans, old_tests = _cfg_test_code(path, old_text, module_file)
            new_spans, new_tests = _cfg_test_code(path, new_text, module_file)
            if gone := sorted((old_tests.keys() - new_tests.keys()) & granted):
                raise ApplyRejected(f"{path} deletes the failing test {gone[0]}; update it to the new behavior instead")
            for lines, spans, tests in ((removed, old_spans, old_tests), (added, new_spans, new_tests)):
                allowed = [span for name, span in tests.items() if name in granted]
                for first, last in spans:
                    touched = sorted(line for line in lines if first <= line <= last
                                     and not any(low <= line <= high for low, high in allowed))
                    if touched:
                        raise ApplyRejected(f"{path}:{touched[0]} is inside a #[cfg(test)] or #[cfg(debug_assertions)] item (lines {first}-{last})")
            old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
            for name in sorted(new_tests.keys() & granted):
                new_fn = "\n".join(new_lines[new_tests[name][0] - 1:new_tests[name][1]])
                granted_texts[name] = new_fn
                if name in kept:
                    if new_fn != parent_texts.get(name):
                        raise ApplyRejected(f"{path} changes the inherited test {name}; only a repair may change a "
                                            "test its parent changed")
                elif name in old_tests:
                    old_fn = "\n".join(old_lines[old_tests[name][0] - 1:old_tests[name][1]])
                    if new_fn != old_fn and (problem := rust.test_weakening(old_fn, new_fn)):
                        raise ApplyRejected(f"{path} weakens the test {name}: {problem}")
            for name in sorted(old_tests.keys() | new_tests.keys()):
                before, after = old_tests.get(name), new_tests.get(name)
                if (before and any(before[0] <= line <= before[1] for line in removed)) \
                        or (after and any(after[0] <= line <= after[1] for line in added)):
                    old_body = old_lines[before[0] - 1:before[1]] if before else []
                    new_body = new_lines[after[0] - 1:after[1]] if after else []
                    changes[name] = "\n".join(difflib.unified_diff(old_body, new_body, f"a/{path}", f"b/{path}",
                                                                  lineterm=""))
        if reverted := sorted(name for name in kept if name in parent_texts and name not in granted_texts):
            raise ApplyRejected(f"the inherited test {reverted[0]} no longer carries its parent's change; only a repair "
                                "may change it")
        return changes, granted_texts
