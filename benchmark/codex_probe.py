"""Zero-spend proof that the Codex tilth-only arm denies shell file reads and has no patch tool.

It builds the argv that ``run._codex_command`` gives a ``tilth_forced`` cell, points it at a local mock
Responses server, and scripts the "model": one ``cat`` of a repo file, then one ``ls``. No provider is
called and no key is read. The report records the request's tool names, whether the hook denied the
``cat``, and whether the ``ls`` ran.

    python benchmark/codex_probe.py --tilth-bin target/debug/tilth --out probe.json

``run.py`` calls ``probe`` and ``problems`` once per run, before any paid Codex tilth-only cell, with the
exact argv a cell uses and the one pinned hook binary.
"""

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run  # noqa: E402

SECRET = "probe-secret-line"
SCRIPT = ("cat hello.txt", "ls")


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


class MockResponses(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        type(self).requests.append(json.loads(raw))
        step = len(type(self).requests) - 1
        if step < len(SCRIPT):
            item = {
                "type": "function_call", "call_id": f"call_{step}", "name": "exec_command",
                "arguments": json.dumps({"cmd": SCRIPT[step]}),
            }
        else:
            item = {
                "type": "message", "role": "assistant", "id": "msg_done",
                "content": [{"type": "output_text", "text": "done"}],
            }
        payload = _sse(
            {"type": "response.created", "response": {"id": f"resp_{step}"}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"id": f"resp_{step}", "usage": {
                "input_tokens": 1, "input_tokens_details": None,
                "output_tokens": 1, "output_tokens_details": None, "total_tokens": 2}}},
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _outputs(request: dict) -> list[str]:
    return [str(item.get("output")) for item in request.get("input", []) if item.get("type") == "function_call_output"]


def probe(mode: run.ModeConfig, hook_binary: Path | str, extra_flags: list[str], keep: Path | None) -> dict:
    """Run the mock-provider cell for ``mode`` with ``hook_binary`` as its hook; return the report.

    Raises ``HookProbeFailed`` first when the hook binary has no working ``hook`` subcommand, so a binary that
    only prints a usage error (exit 2, which reads as a deny) never passes as an enforcing hook.
    """
    run._probe_hook(str(hook_binary), "tilth", host="codex")
    MockResponses.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockResponses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch) / "repo"
        repo.mkdir()
        (repo / "hello.txt").write_text(SECRET + "\n")
        home = Path(scratch) / "codex_home"
        home.mkdir()
        task = SimpleNamespace(prompt="Read hello.txt", hide_git=True)
        cmd = run._codex_command(task, mode, "gpt-5.6-sol", repo, reasoning_effort=None, skill_paths=[],
                              hook_binary=str(hook_binary))
        provider = [
            "-c", 'model_provider="mock"',
            "-c", 'model_providers.mock.name="mock"',
            "-c", f'model_providers.mock.base_url="http://127.0.0.1:{server.server_port}/v1"',
            "-c", 'model_providers.mock.wire_api="responses"',
            "-c", 'model_providers.mock.env_key="OPENAI_MOCK_KEY"',
            "-c", "model_providers.mock.supports_websockets=false",
            "-c", "model_providers.mock.request_max_retries=0",
            "-c", "model_providers.mock.stream_max_retries=0",
        ]
        split = cmd.index("--")
        argv = [*cmd[:split], *extra_flags, *provider, *cmd[split:]]
        env = {
            "PATH": run.os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home),
            "OPENAI_MOCK_KEY": "mock",
        }
        surface = sorted(run.check_codex_tool_surface(cmd, env, repo))
        done = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if keep is not None:
            keep.mkdir(parents=True, exist_ok=True)
            (keep / "codex.stdout.jsonl").write_text(done.stdout)
            (keep / "codex.stderr.txt").write_text(done.stderr)
            (keep / "requests.json").write_text(json.dumps(MockResponses.requests, indent=1))
    server.shutdown()
    items = [event["item"] for line in done.stdout.splitlines() if line.startswith("{")
             for event in [json.loads(line)] if event.get("type") == "item.completed"]
    requests = MockResponses.requests
    request_tools = sorted(run._codex_tool_names(requests[0].get("input", []))) if requests else []
    outputs = _outputs(requests[-1]) if requests else []
    return {
        "argv_has_catalog": any(part.startswith(run._CODEX_CATALOG_KEY) for part in argv),
        "argv_has_hook": any(part.startswith("hooks.PreToolUse=") for part in argv),
        "argv_disables_hooks": any(argv[i:i + 2] == ["--disable", "hooks"] for i in range(len(argv))),
        "argv_enables_hooks": any(argv[i:i + 2] == ["--enable", "hooks"] for i in range(len(argv))),
        "extra_flags": extra_flags,
        "returncode": done.returncode,
        "preflight_tools": surface,
        "request_count": len(requests),
        "request_tools": request_tools,
        "apply_patch_in_request": "apply_patch" in request_tools,
        "tool_outputs": outputs,
        "cat_denied": any("Blocked" in out for out in outputs[:1]) and not any(SECRET in out for out in outputs),
        "secret_leaked": any(SECRET in out for out in outputs),
        "ls_ran": any("hello.txt" in out and "Blocked" not in out for out in outputs[1:]),
        "item_types": [item.get("type") for item in items],
    }


def problems(report: dict) -> list[str]:
    """Name every way the probe report shows the hook or catalog not enforcing; empty means enforcing."""
    found = []
    if not report["argv_enables_hooks"] or report["argv_disables_hooks"]:
        found.append("argv does not enable hooks")
    if not report["cat_denied"]:
        found.append("the hook did not deny a repo-file cat")
    if report["secret_leaked"]:
        found.append("a repo file's content reached the model")
    if not report["ls_ran"]:
        found.append("a benign command did not run")
    if report["apply_patch_in_request"]:
        found.append("apply_patch is in the request")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tilth-bin", type=Path, required=True)
    parser.add_argument("--hook-bin", type=Path, help="hook binary; defaults to --tilth-bin")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep", type=Path, help="directory for the raw stream, stderr and request bodies")
    parser.add_argument("--extra", action="append", default=[], help="extra codex exec flag, repeatable")
    args = parser.parse_args()
    tilth_bin = args.tilth_bin.resolve()
    mode = replace(run.MODES["tilth_forced"], binary_path=str(tilth_bin))
    report = probe(mode, (args.hook_bin or tilth_bin).resolve(), args.extra, args.keep)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 1 if problems(report) else 0


if __name__ == "__main__":
    sys.exit(main())
