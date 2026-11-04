"""The ``src_patch`` proposer: an isolated ``claude -p`` that edits an export of the candidate's own tree."""

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from parse import extract_trajectory

from .calls import PaidCalls
from .candidate import OBJECTIVE, SRC_PATCH, TEXT_COMPONENTS
from .diagnosis import DIAGNOSIS_NOTE, DIRECTIVE_NOTE, PROPOSER_HYPOTHESIS, HypothesisBook
from .gitops import GitError, git, git_bytes
from .rustfmt import format_tree

EXPORT_PATHS = ("src", "prompts", "Cargo.toml", "Cargo.lock")
TOOLS = "Read,Edit,Write,Glob,Grep"
DISALLOWED = "Bash,WebFetch,WebSearch,Agent,Task"
PATH_KEYS = ("file_path", "path", "notebook_path")
REPAIR_NOTE = ("A record with a `repair` key means your earlier change failed. When its `stage` is `just check`, "
               "that change is already applied here and `error` holds the compiler or lint output. When its `stage` "
               "is `apply`, the tree is the parent's and `failed_diff` holds the change that did not apply. When its "
               "`stage` is `integrity`, the seed's existing tests ran against your production code, exactly as the seed "
               "wrote them, and failed or went missing; fix the production code and do not edit those tests. Fix only "
               "that failure and keep the intended change. When the repair record has `may_update_tests`, the "
               "`just check` failure includes the unit tests in `failing_tests`, and you may change only those test "
               "fns, and only when your intended behavior change makes a pinned test obsolete. Update each such test "
               "to assert the NEW intended behavior. Never delete a test, and never weaken it so that it passes "
               "trivially; a person reviews every test change. If a test fails for another reason, fix the code.")
PROPOSER_HEADER = (
    "You are improving tilth, a Rust MCP server for code intelligence whose source is in the current "
    "directory. Below are reflective records of AI agents' rollouts with tilth on benchmark tasks: each holds "
    "the task prompt, the rollout's row fields, and its full tool-call trajectory, and may carry an "
    f"applicability label and a critique. {OBJECTIVE} Edit files under src/ so agents solve tasks like these "
    "at lower cost without losing correctness. Edit nothing outside src/, keep the crate building, and do not "
    "change or remove existing #[cfg(test)] code or seed #[test] fns, except as the repair record allows. You may "
    "add new #[test] fns that test your new behavior; each needs a test attribute and no `ignore`, and a person "
    "reviews every one. After `just check` passes, the seed's own tests also run against your production code, "
    "exactly as the seed wrote them, in a tree without your test code. All of them must still exist and pass, so "
    "do not rely on a helper, macro, or `use` that you add to test code. "
    f"{DIAGNOSIS_NOTE} {DIRECTIVE_NOTE} {REPAIR_NOTE} {PROPOSER_HYPOTHESIS}"
)


def proposer_prompt(records: list[dict]) -> str:
    """The proposer prompt: a fixed header and the reflective records, nothing else."""
    return f"{PROPOSER_HEADER}\n\n<records>\n{json.dumps(records, indent=2, sort_keys=True)}\n</records>\n"


def _snapshot(root: Path) -> dict[str, str]:
    """Hash every file outside ``src/``, to spot proposer edits that are dropped."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file() and path.relative_to(root).parts[0] != "src"
    }


class Proposer:
    """``propose_src_patch(candidate, records) -> diff``, cumulative against the seed's ``src/**``.

    The proposer sees only an export of ``src/``, ``prompts/``, ``Cargo.toml``, and
    ``Cargo.lock`` at the seed with the candidate's texts and cumulative patch
    applied, in a temp directory with no ``.git``. A trajectory that reaches outside
    the export or names ``benchmark/``, the panel file, or the harness data
    directory is rejected, and the parent's ``src_patch`` comes back unchanged.
    """

    def __init__(self, repo: Path, seed_sha: str, paid: PaidCalls, model: str, *, forbidden: list[str],
                 log: Callable[[str], None], hypotheses: HypothesisBook | None = None,
                 fmt: Callable[[Path], object] | None = None) -> None:
        self.fmt = fmt or format_tree
        self.hypotheses = hypotheses
        self.repo = Path(repo)
        self.seed_sha = seed_sha
        self.paid = paid
        self.model = model
        self.forbidden = [term for term in dict.fromkeys(forbidden) if term]
        self.log = log

    def argv(self) -> list[str]:
        return ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
                "--tools", TOOLS, "--disallowedTools", DISALLOWED, "--permission-mode", "acceptEdits",
                "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                "--disable-slash-commands"]

    @contextmanager
    def export(self, candidate: Mapping[str, str]) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="tilth-evolve-export-") as scratch:
            root = Path(scratch) / "tilth"
            root.mkdir()
            archive = git_bytes("archive", "--format=tar", self.seed_sha, *EXPORT_PATHS, cwd=self.repo)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(root, filter="data")
            for name in TEXT_COMPONENTS:
                if name in candidate:
                    (root / name).write_bytes(candidate[name].encode("utf-8"))
            if candidate[SRC_PATCH].strip():
                git("apply", "--whitespace=nowarn", "-", cwd=root, input=candidate[SRC_PATCH])
            yield root

    def propose_src_patch(self, candidate: Mapping[str, str], records: list[dict]) -> str:
        parent = candidate[SRC_PATCH]
        try:
            with self.export(candidate) as root:
                return self._propose(root, parent, records)
        except GitError as error:
            self.log(f"proposer: {error}; keeping the parent's src_patch")
            return parent

    def _propose(self, root: Path, parent: str, records: list[dict]) -> str:
        before = _snapshot(root)
        outcome = self.paid.call("proposer", self.argv(), proposer_prompt(records), root)
        if outcome is None:
            return parent
        reason = self.scan(extract_trajectory(outcome.stream, "claude"), root)
        if reason is not None:
            self.log(f"proposer: rejected ({reason}); keeping the parent's src_patch")
            return parent
        if self.hypotheses is not None:
            self.hypotheses.offer(SRC_PATCH, outcome.text)
        after = _snapshot(root)
        dropped = sorted(path for path in {*before, *after} if before.get(path) != after.get(path))
        if dropped:
            self.log(f"proposer: dropped edits outside src/**: {', '.join(dropped)}")
        return self.cumulative_diff(root)

    def scan(self, calls: list[dict], root: Path) -> str | None:
        """Why a proposer trajectory is rejected, or None when it stayed inside the export."""
        export = os.path.realpath(root)
        for call in calls:
            name, tool_input = call.get("name"), call.get("input")
            if isinstance(tool_input, dict):
                paths = [tool_input.get(key) for key in PATH_KEYS]
                if name == "Glob" and isinstance(tool_input.get("pattern"), str):
                    # Resolve ``..`` first: one after a wildcard (``**/../..``) still climbs out of the export.
                    base = os.path.join(export, os.path.expanduser(tool_input.get("path") or ""))
                    pattern = os.path.normpath(os.path.join(base, os.path.expanduser(tool_input["pattern"])))
                    paths.append(re.split(r"[*?\[{]", pattern, maxsplit=1)[0] or os.sep)
                for value in paths:
                    if not isinstance(value, str) or not value:
                        continue
                    resolved = os.path.realpath(os.path.join(export, os.path.expanduser(value)))
                    if resolved != export and not resolved.startswith(export + os.sep):
                        return f"{name} reached {value}, outside the export"
            text = json.dumps(tool_input) + json.dumps(call.get("output"))
            for term in self.forbidden:
                if term in text:
                    return f"{name} named {term}"
        return None

    def cumulative_diff(self, root: Path) -> str:
        """The export's ``src/**`` against the seed's, as a unified diff ``git apply`` accepts at the seed.

        The export is rustfmt-formatted first, so the diff, its content id, and the next proposal's base are all
        formatted code.
        """
        if self.fmt(root) is False:
            # `cargo fmt` exits non-zero on a parse error; the patch then stays unformatted and `just check` says why.
            self.log("proposer: cargo fmt failed on the export; the diff stays unformatted")
        common = Path(git("rev-parse", "--git-common-dir", cwd=self.repo).strip())
        git_dir = common if common.is_absolute() else self.repo / common
        with tempfile.TemporaryDirectory(prefix="tilth-evolve-index-") as index_dir:
            env = {"GIT_INDEX_FILE": str(Path(index_dir) / "index")}
            base = (f"--git-dir={git_dir}", f"--work-tree={root}")
            git(*base, "read-tree", self.seed_sha, cwd=root, env=env)
            git(*base, "add", "-A", "--", "src", cwd=root, env=env)
            return git(*base, "diff", "--cached", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/",
                       self.seed_sha, "--", "src", cwd=root, env=env)
