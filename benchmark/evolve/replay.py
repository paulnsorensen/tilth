"""Diagnostic replay of a recorded cell's tilth tool calls against a tilth binary; makes no model calls.

The evolve loop pays minutes and dollars per candidate to learn whether a ``src/`` change shrinks tool
output. This module re-sends a seed cell's recorded ``tilth_*`` calls to one persistent ``tilth --mcp``
server in a fresh fixture workspace and measures result size and error status. It never rejects a
candidate: ``replay_cell`` reports, ``compare`` diffs two reports, and nothing here is wired into the loop.

Recorded Bash is never executed. A Bash call the recorded run never ran (a hook blocked it) or one on a
small read-only allowlist is skipped; any other Bash call ends the replay, because its file effects are
unknown and later calls may depend on them. Tags are never repaired and reads are never inserted.
"""

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

TOOL_PREFIX = "mcp__tilth__"
TILTH_TOOLS = frozenset({"tilth_read", "tilth_search", "tilth_write"})
RECORDED_ROOT = re.compile(r"/tmp/tilth-benchmark-[A-Za-z0-9_]+/[^/\s\"'\\:;]+")
PATH_KEYS = frozenset({"cwd", "path", "dest", "scope"})
HOOK_BLOCKED = "PreToolUse:Bash hook error"
CALL_TIMEOUT_S = 120
# Claude Code replaces an oversized tool result with a short preview wrapped in this marker.
PERSISTED_MARKER = "<persisted-output>"
OLD_MTIME = 946684800  # 2000-01-01: older than every recorded `if_modified_since`, so unchanged files stay unchanged

READ_ONLY_COMMANDS = frozenset({"cd", "ls", "pwd", "git", "find", "head", "tail", "cut", "wc"})
GIT_READ_ONLY = frozenset({"status", "diff", "log", "show"})
FIND_FORBIDDEN = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})


def _text(output: object) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(block.get("text") or "" for block in output if isinstance(block, dict))
    return json.dumps(output)


def _text_len(output: object) -> int:
    return len(_text(output))


def read_only_bash(command: str) -> bool:
    """True when every simple command in ``command`` is on the audited read-only allowlist.

    Redirection, background jobs, substitution, and subshells are refused outright, so the check cannot
    pass a command that writes. The caller never runs the command either way.
    """
    if "`" in command or "$(" in command or "\n" in command.strip():
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    simple: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            simple.append([])
        elif set(token) <= set("();&|<>"):
            return False
        else:
            simple[-1].append(token)
    for argv in simple:
        if not argv:
            continue
        name = argv[0]
        if name not in READ_ONLY_COMMANDS:
            return False
        if name == "git":
            rest = [arg for arg in argv[1:] if arg != "-C" and not arg.startswith("/")]
            sub = next((arg for arg in rest if not arg.startswith("-")), None)
            if sub not in GIT_READ_ONLY or any(arg.startswith("--output") for arg in argv):
                return False
        if name == "find" and FIND_FORBIDDEN & set(argv):
            return False
    return True


def bash_disposition(call: dict) -> str | None:
    """``blocked`` or ``allowlisted`` when the Bash call is safe to skip, else None."""
    output = call.get("output")
    text = output if isinstance(output, str) else ""
    if call.get("is_error") and text.startswith(HOOK_BLOCKED):
        return "blocked"
    command = (call.get("input") or {}).get("command")
    if isinstance(command, str) and read_only_bash(command):
        return "allowlisted"
    return None


def recorded_root(calls: list[dict]) -> str | None:
    """The recorded workspace root: the most common ``/tmp/tilth-benchmark-*/<repo>`` prefix in the calls."""
    found = Counter(RECORDED_ROOT.findall(json.dumps([call.get("input") for call in calls])))
    return found.most_common(1)[0][0] if found else None


def remap(value: object, old: str, new: str, key: str = "") -> object:
    """Swap the recorded root for the new one in path fields only; edit content and queries stay verbatim."""
    if isinstance(value, str):
        return value.replace(old, new) if key in PATH_KEYS or key == "paths" else value
    if isinstance(value, list):
        return [remap(item, old, new, key) for item in value]
    if isinstance(value, dict):
        return {name: remap(item, old, new, name) for name, item in value.items()}
    return value


def unremapped(value: object, new: str, key: str = "") -> list[str]:
    """Absolute paths in path fields that sit outside the new workspace; these were not remapped."""
    found: list[str] = []
    if isinstance(value, str):
        absolute = value.startswith("/") and not value.startswith("//")
        if (key in PATH_KEYS or key == "paths") and absolute and not (value == new or value.startswith(new + "/")):
            found.append(value)
    elif isinstance(value, list):
        for item in value:
            found += unremapped(item, new, key)
    elif isinstance(value, dict):
        for name, item in value.items():
            found += unremapped(item, new, name)
    return found


def _touched(arguments: dict, workspace: Path) -> set[str]:
    """Workspace-relative paths a ``tilth_write`` call names, including move destinations."""
    base = Path(arguments.get("cwd") or workspace)
    names: set[str] = set()
    for section in arguments.get("edits") or []:
        if not isinstance(section, dict):
            continue
        candidates = [section.get("path")]
        candidates += [op.get("dest") for op in section.get("ops") or [] if isinstance(op, dict)]
        for name in candidates:
            if isinstance(name, str):
                resolved = Path(os.path.normpath(base / name))
                if resolved == workspace or workspace in resolved.parents:
                    names.add(resolved.relative_to(workspace).as_posix())
    return names


def _section_failed(write_result: str) -> bool:
    """A multi-section write reports each rejected section as `error` while the call itself is not an error."""
    return re.search(r"^## .+\nerror", write_result, re.MULTILINE) is not None


def _hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class McpServer:
    """One persistent ``tilth --mcp`` process speaking line-delimited JSON-RPC over stdio."""

    def __init__(self, binary: Path, cwd: Path, log: Path) -> None:
        self._log = log.open("wb")
        self._proc = subprocess.Popen(
            [str(binary), "--mcp", "--edit"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, bufsize=1,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0
        self.request("initialize", {})

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def request(self, method: str, params: dict) -> dict:
        assert self._proc.stdin is not None
        self._next_id += 1
        request_id = self._next_id
        try:
            self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            self._proc.stdin.flush()
        except OSError as error:
            raise RuntimeError(f"server stdin closed: {error}") from error
        while True:
            try:
                line = self._lines.get(timeout=CALL_TIMEOUT_S)
            except queue.Empty as error:
                raise RuntimeError(f"no response to {method} within {CALL_TIMEOUT_S}s") from error
            if line is None:
                raise RuntimeError("server exited before responding")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """The joined result text and whether the server flagged it an error."""
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            return str(response["error"].get("message", response["error"])), True
        result = response.get("result") or {}
        blocks = result.get("content") or []
        text = "".join(block.get("text") or "" for block in blocks if isinstance(block, dict))
        return text, bool(result.get("isError"))

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()
            self._proc.wait()
        finally:
            self._log.close()


def prepare_workspace(row: dict, workdir: Path) -> Path:
    """A fresh checkout of the task's fixture at its pinned revision, without grading assets or tests run.

    A local task clones its fixture repo (leaving that repo's metadata untouched) and applies its
    mutations; an external task exports its prepared tree. The workspace never carries a built
    environment: replay only needs source files.
    """
    import external
    import run

    task = run.TASKS.get(row["task"])
    if task is not None and task.fixture_path is not None:
        source = run.get_repo_path(task.repo)
        workspace = workdir / source.name
        revision = (row.get("source") or {}).get("commit_or_tag") or "HEAD"
        for argv in (
            ["git", "clone", "--quiet", "--no-checkout", "--local", str(source), str(workspace)],
            ["git", "-C", str(workspace), "checkout", "--quiet", "--detach", revision],
        ):
            subprocess.run(argv, check=True, capture_output=True, text=True)
        task.prepare(workspace)
        if getattr(task, "hide_git", False):
            shutil.rmtree(workspace / ".git", ignore_errors=True)
        return workspace
    external_task, _verdict = external.resolve_task(row["task"])
    if external_task is None:
        raise LookupError(f"unknown task {row['task']!r}")
    workspace = workdir / "repo"
    external_task.export_prepared(workspace)
    return workspace


def _age_files(workspace: Path) -> None:
    for directory, dirs, files in os.walk(workspace):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in files:
            try:
                os.utime(Path(directory) / name, (OLD_MTIME, OLD_MTIME), follow_symlinks=False)
            except OSError:
                pass


def load_trajectory(row: dict) -> list[dict]:
    path = row.get("trajectory_path")
    if not path or not Path(path).is_file():
        raise FileNotFoundError(f"trajectory missing for {row.get('task')}: {path}")
    calls = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls


def replay_cell(row: dict, binary: Path, *, workdir: Path) -> dict:
    """Replay the row's recorded tilth calls against ``binary`` in a fresh workspace under ``workdir``."""
    trajectory = load_trajectory(row)
    workdir.mkdir(parents=True, exist_ok=True)
    workspace = prepare_workspace(row, workdir).resolve()
    _age_files(workspace)
    old_root = recorded_root(trajectory)
    new_root = str(workspace)
    report: dict = {
        "task": row["task"], "total_calls": len(trajectory), "replayed_calls": 0, "skipped_bash": 0,
        "stopped_at": None, "unremapped_paths": [], "calls": [], "file_hashes": {},
    }
    touched: set[str] = set()
    server = McpServer(Path(binary).resolve(), workspace, workdir / "server.stderr")
    try:
        for index, call in enumerate(trajectory):
            name = str(call.get("name") or "")
            if name == "Bash":
                if bash_disposition(call) is None:
                    report["stopped_at"] = {"index": index, "reason": "bash outside the read-only allowlist"}
                    break
                report["skipped_bash"] += 1
                continue
            tool = name.removeprefix(TOOL_PREFIX)
            if not name.startswith(TOOL_PREFIX) or tool not in TILTH_TOOLS:
                report["stopped_at"] = {"index": index, "reason": f"tool {name!r} is not replayable"}
                break
            arguments = call.get("input") or {}
            if old_root:
                arguments = remap(arguments, old_root, new_root)
            report["unremapped_paths"] += unremapped(arguments, new_root)
            recorded_error = bool(call.get("is_error")) or (tool == "tilth_write" and _section_failed(_text(call.get("output"))))
            if tool == "tilth_write":
                if call.get("output") is None:
                    report["stopped_at"] = {"index": index, "reason": "recorded write has no result"}
                    break
                touched |= _touched(arguments, workspace)
            try:
                text, replayed_error = server.call(tool, arguments)
                replayed_error = replayed_error or (tool == "tilth_write" and _section_failed(text))
            except RuntimeError as error:
                report["stopped_at"] = {"index": index, "reason": f"server failure: {error}"}
                break
            if old_root:
                text = text.replace(new_root, old_root)
            report["replayed_calls"] += 1
            report["calls"].append({
                "index": index, "tool": tool, "recorded_chars": _text_len(call.get("output")),
                "replayed_chars": len(text), "recorded_error": recorded_error, "replayed_error": replayed_error,
                "recorded_truncated": PERSISTED_MARKER in _text(call.get("output")),
            })
            if tool == "tilth_write" and replayed_error != recorded_error:
                report["stopped_at"] = {
                    "index": index,
                    "reason": f"divergent write: recorded error={recorded_error}, replayed error={replayed_error}: {text[:300]}",
                }
                break
    finally:
        server.close()
    report["file_hashes"] = {name: _hash(workspace / name) for name in sorted(touched)}
    report["unremapped_paths"] = sorted(set(report["unremapped_paths"]))
    return report


def compare(seed: dict, candidate: dict) -> dict:
    """Candidate minus seed, over the call indices both replays reached; diagnostic only.

    ``files_match`` is None when the seed touched no file or the two replays stopped at different
    points, since the file states are then not comparable.
    """
    seed_calls = {call["index"]: call for call in seed["calls"]}
    candidate_calls = {call["index"]: call for call in candidate["calls"]}
    delta: dict[str, int] = {}
    new_errors: set[str] = set()
    for index in seed_calls.keys() & candidate_calls.keys():
        before, after = seed_calls[index], candidate_calls[index]
        delta[after["tool"]] = delta.get(after["tool"], 0) + after["replayed_chars"] - before["replayed_chars"]
        if after["replayed_error"] and not before["replayed_error"]:
            new_errors.add(after["tool"])
    comparable = bool(seed["file_hashes"]) and seed.get("stopped_at") == candidate.get("stopped_at")
    return {
        "coverage": candidate["replayed_calls"] / candidate["total_calls"] if candidate["total_calls"] else 0.0,
        "result_chars_delta_by_tool": dict(sorted(delta.items())),
        "new_error_tools": sorted(new_errors),
        "files_match": seed["file_hashes"] == candidate["file_hashes"] if comparable else None,
    }


def _chars_by_tool(report: dict, key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for call in report["calls"]:
        totals[call["tool"]] = totals.get(call["tool"], 0) + call[key]
    return totals


def _cells(rows_path: Path, task: str | None, limit: int) -> list[dict]:
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
    chosen = [
        row for row in rows
        if row.get("mode") == "tilth_forced" and (task is None or row.get("task") == task)
        and row.get("trajectory_path") and Path(row["trajectory_path"]).is_file()
    ]
    return chosen[:limit]


def _stop_text(report: dict) -> str:
    stop = report["stopped_at"]
    return "-" if stop is None else f"{stop['index']}: {stop['reason'][:60]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--task")
    parser.add_argument("--binary", type=Path, required=True, help="the seed tilth binary")
    parser.add_argument("--candidate-binary", type=Path)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args(argv)
    cells = _cells(args.rows, args.task, args.limit)
    if not cells:
        print("no tilth_forced cell with a readable trajectory matched", file=sys.stderr)
        return 1
    print(f"{'task':40} rep cover  recorded  replayed  trunc  stop")
    for row in cells:
        with tempfile.TemporaryDirectory(prefix="tilth-replay-") as temp:
            seed = replay_cell(row, args.binary, workdir=Path(temp) / "seed")
            whole = [call for call in seed["calls"] if not call["recorded_truncated"]]
            recorded = sum(call["recorded_chars"] for call in whole)
            replayed = sum(call["replayed_chars"] for call in whole)
            truncated = len(seed["calls"]) - len(whole)
            cover = f"{seed['replayed_calls']}/{seed['total_calls']}"
            print(f"{row['task'][:40]:40} {row.get('repetition', '?'):>3} {cover:>5} {recorded:>9} {replayed:>9} {truncated:>6}  {_stop_text(seed)}")
            if seed["unremapped_paths"]:
                print(f"  unremapped paths: {seed['unremapped_paths']}")
            if args.candidate_binary:
                candidate = replay_cell(row, args.candidate_binary, workdir=Path(temp) / "candidate")
                diff = compare(seed, candidate)
                print(f"  candidate coverage={diff['coverage']:.2f} delta={diff['result_chars_delta_by_tool']} "
                      f"new_error_tools={diff['new_error_tools']} files_match={diff['files_match']} stop={_stop_text(candidate)}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
