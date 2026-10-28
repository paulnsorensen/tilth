"""The integrity stage: the seed's tests, exactly as the seed wrote them, run against the candidate's production code.

A text rule cannot tell whether an added test or macro changes what an existing test checks. This stage does not
read the candidate's test code at all. It builds a tree from the candidate's production code and the seed's test
code, and runs that.

Tree construction, per ``src/**/*.rs`` file:

* A seed test file (a ``#[cfg(test)] mod x;`` file, or a file with an inner ``#![cfg(..test..)]``) comes back
  byte for byte from the seed. A test file the candidate adds is deleted.
* In any other file, each outermost ``cfg`` item that names ``test`` or ``debug_assertions`` is swapped, in place,
  for the seed item with the same header. A candidate item without a seed twin is dropped. A seed item without a
  candidate twin is appended at the end of the file. The swap is in place, so an attribute the candidate stacks
  above the item (``#[cfg(any())]``) still sits on the seed's item and compiles it out, which the name check sees.
* A test fn named in ``granted`` takes the candidate's version inside the seed's item. The candidate's version
  must stand alone on its lines, so nothing can ride along on them.
* The byte-lock literals follow the candidate's prompt, as the applier rewrites them.

The tree is refused when the candidate's production code defines a ``macro_rules!`` whose name the seed already
invokes (a shadow of ``assert_eq!``), or when a candidate file gains an inner ``#![cfg(..test..)]``.

Then every seed test name must be listed, as many times as the seed lists it, and every test must pass.
"""

import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from . import rust
from .gitops import git
from .materialize import BYTE_LOCK_FILE

Cargo = Callable[[Path, list[str]], tuple[bool, str]]
LIST_ARGS = ["test", "--lib", "--bins", "--", "--list"]
TEST_ARGS = ["test", "--lib", "--bins"]
HEADLINE = "Existing seed tests failed against the new production code, which ran them exactly as the seed wrote them."
SHOWN = 10
_LISTED = re.compile(r"(\S.*): test")


class Unsound(Exception):
    """The candidate's code has a shape whose test code the integrity tree cannot replace soundly."""


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _names(listing: str) -> Counter[str]:
    """The test names in ``cargo test -- --list`` output; lib and bin targets can repeat a name."""
    return Counter(found[1] for line in listing.splitlines() if (found := _LISTED.fullmatch(line)))


def _shown(names: list[str]) -> str:
    if not names:
        return "see the output above"
    more = f", and {len(names) - SHOWN} more" if len(names) > SHOWN else ""
    return ", ".join(names[:SHOWN]) + more


def _test_files(texts: dict[str, str]) -> set[str]:
    """Files that hold only test code: the ``#[cfg(test)] mod x;`` files and files with an inner test ``cfg``."""
    files: set[str] = set()
    for path, text in texts.items():
        files |= rust.test_module_files(path, text)
        if any(inner for _start, _end, _key, inner in rust.cfg_test_regions(text)):
            files.add(path)
    return files


def _granted_seed(seed: dict[str, str], candidate: dict[str, str], granted: frozenset[str]) -> dict[str, str]:
    """``seed`` with each granted test fn replaced by the candidate's version of it."""
    wanted: dict[str, str] = {}
    for path, text in candidate.items():
        lines = text.split("\n")
        for name, first, last in rust.test_functions(path, text):
            if name in granted and name not in wanted:
                fn = "\n".join(lines[first - 1:last])
                if not rust.is_standalone_fn(fn):
                    raise Unsound(f"the granted test {name} shares its lines with other code")
                wanted[name] = fn
    result = dict(seed)
    for path, text in seed.items():
        lines = text.split("\n")
        for name, first, last in sorted(rust.test_functions(path, text), key=lambda found: -found[1]):
            if name in granted:
                if name not in wanted:
                    raise Unsound(f"the granted test {name} is missing from the candidate")
                lines[first - 1:last] = wanted[name].split("\n")
        result[path] = "\n".join(lines)
    return result


def _swap_regions(candidate: str, seed: str) -> str:
    """``candidate`` with each outermost test ``cfg`` item swapped for the seed's item with the same header."""
    seed_regions = rust.cfg_test_regions(seed)
    pool: dict[str, list[int]] = {}
    for index, (_start, _end, key, _inner) in enumerate(seed_regions):
        pool.setdefault(key, []).append(index)
    used: set[int] = set()
    pieces, cursor = [], 0
    for start, end, key, inner in rust.cfg_test_regions(candidate):
        if inner:
            raise Unsound("a production file gains an inner #![cfg(..test..)] attribute")
        pieces.append(candidate[cursor:start])
        if pool.get(key):
            index = pool[key].pop(0)
            used.add(index)
            pieces.append(seed[seed_regions[index][0]:seed_regions[index][1]])
        cursor = end
    pieces.append(candidate[cursor:])
    text = "".join(pieces)
    leftover = [seed[start:end] for index, (start, end, _key, _inner) in enumerate(seed_regions) if index not in used]
    if leftover:
        text = text.rstrip("\n") + "\n\n" + "\n\n".join(leftover) + "\n"
    return text


class Integrity:
    """``check(worktree, granted) -> (ok, output)``. ``cargo(tree, args)`` runs cargo in ``tree``."""

    def __init__(self, repo: Path, seed_sha: str, scratch: Path, cargo: Cargo) -> None:
        self.repo = Path(repo)
        self.seed_sha = seed_sha
        self.scratch = Path(scratch)
        self.cargo = cargo
        self._texts: dict[str, str] | None = None
        self._names: Counter[str] | None = None

    def _run(self, tree: Path, args: list[str]) -> tuple[bool, str]:
        """Run cargo after a touch of the sources. Trees share one target dir, and cargo trusts file mtimes.

        Without the touch, a build of another tree can leave a dep-info file newer than this tree's sources.
        Cargo then reuses that tree's binary and runs the wrong tests.
        """
        for source in Path(tree).rglob("*.rs"):
            if "target" not in source.relative_to(tree).parts:
                source.touch()
        return self.cargo(tree, args)

    def check(self, worktree: Path, granted: frozenset[str] = frozenset()) -> tuple[bool, str]:
        """Build the integrity tree in ``worktree``, which the caller releases afterward, and run it."""
        worktree = Path(worktree)
        try:
            self._build(worktree, granted)
        except Unsound as unsound:
            return False, f"{HEADLINE}\nThe candidate's test code cannot be replaced soundly: {unsound}"
        ok, listing = self._run(worktree, LIST_ARGS)
        if not ok:
            return False, f"{listing}\n{HEADLINE} The seed's tests do not compile against it."
        have = _names(listing)
        missing = sorted(name for name, count in self.seed_names().items() if have[name] < count)
        if missing:
            return False, (f"{HEADLINE}\n{len(missing)} seed tests are missing (compiled out or removed): "
                           f"{_shown(missing)}")
        ok, output = self._run(worktree, TEST_ARGS)
        if not ok:
            return False, f"{output}\n{HEADLINE} Failed: {_shown(rust.failing_tests(output))}"
        return True, ""

    def seed_names(self) -> Counter[str]:
        """The seed's test names, listed once per run in a scratch checkout of the seed."""
        if self._names is None:
            tree = self.scratch / "integrity-seed"
            shutil.rmtree(tree, ignore_errors=True)
            tree.parent.mkdir(parents=True, exist_ok=True)
            git("worktree", "add", "--detach", str(tree), self.seed_sha, cwd=self.repo)
            try:
                ok, listing = self._run(tree, LIST_ARGS)
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=self.repo, capture_output=True)
                shutil.rmtree(tree, ignore_errors=True)
            if not ok:
                raise RuntimeError(f"cannot list the seed's tests:\n{listing[-2000:]}")
            self._names = _names(listing)
        return self._names

    def _seed_texts(self) -> dict[str, str]:
        if self._texts is None:
            names = git("ls-tree", "-r", "--name-only", self.seed_sha, "--", "src", cwd=self.repo).split("\n")
            self._texts = {name: git("show", f"{self.seed_sha}:{name}", cwd=self.repo)
                           for name in names if name.endswith(".rs")}
        return self._texts

    def _build(self, worktree: Path, granted: frozenset[str]) -> None:
        seed = self._seed_texts()
        candidate = {path.relative_to(worktree).as_posix(): _read(path)
                     for path in sorted((worktree / "src").rglob("*.rs"))}
        seed_tests = _granted_seed(seed, candidate, granted)
        seed_files = _test_files(seed)
        added_files = _test_files(candidate) - seed_files - seed.keys()
        final: dict[str, str | None] = {}
        for path in candidate.keys() | seed.keys():
            if path in seed_files:
                final[path] = seed_tests[path]
            elif path in candidate:
                final[path] = None if path in added_files else _swap_regions(candidate[path], seed_tests.get(path, ""))
        lock = final.get(BYTE_LOCK_FILE)
        if lock is not None and rust.BYTE_LOCK_FN in lock:
            try:
                final[BYTE_LOCK_FILE] = rust.rewrite_byte_lock(lock, _read(worktree / "prompts" / "mcp.md"))
            except (ValueError, OSError) as error:
                raise Unsound(f"{BYTE_LOCK_FILE}: {error}") from error
        defined = set().union(*(rust.macro_names(text) for text in final.values() if text))
        known = set().union(*(rust.macro_names(text) for text in seed.values()))
        called = set().union(*(rust.invoked_macros(text) for text in seed.values()))
        if shadowed := sorted((defined - known) & called):
            raise Unsound(f"the production code defines the macro {shadowed[0]}!, which the seed's code already "
                          "invokes, so it can change what the seed's tests check")
        for path, text in final.items():
            target = worktree / path
            if text is None:
                target.unlink(missing_ok=True)
            elif not target.is_file() or _read(target) != text:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode("utf-8"))
