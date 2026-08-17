"""Stdio JSON-RPC test harness for the tilth --mcp binary (search-v2 oracle)."""
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = REPO_ROOT / "target" / "debug" / "tilth"

WITNESS = {
    "AC-1": "A surface value advertises a tool outside its defined set, or absent flag ≠ v1",
    "AC-2": "Both-surface omits v2, changes list, or exposes an unapproved verb",
    "AC-3": "Mixed per-query paths reorder/drop results or invalid batch accepted",
    "AC-4": "A deterministic fixture resolves through the wrong precedence or needs `kind",
    "AC-5": "Response invalid/missing envelope fields or leaks `routes_tried`; telemetry omits route",
    "AC-6": "Unique symbol lacks bounded core/impact, omitted deep sections lack typed continuations, or ambiguity deep-expands",
    "AC-7": "Stale edge appears, core search fails, or work survives past sub-deadline",
    "AC-8": "Profiles/worktrees share a DB, path unstable, or changed state falsely complete",
    "AC-9": "clientInfo.name ignored (today's behavior) or absent clientInfo panics/unstable key",
    "AC-10": "Any predeclared gate fails yet verdict reads pass, or verdict artifact unparseable",
    "AC-11": "Record missing required field, contains source content, or file grows unbounded",
    "AC-12": "Evaluator reports pass with a missing/`[BLOCKED]` floor or unmet threshold",
    "AC-13": "v1 surface drifts from 13,779 baseline or trial surface exceeds re-baselined cap",
    "AC-14": "Library behavior or list behavior changes",
}


def build_if_needed():
    if not BIN.exists():
        subprocess.run(["cargo", "build", "--bin", "tilth"], cwd=REPO_ROOT, check=True)


class Mcp:
    def __init__(self, returncode, responses, stdout, stderr):
        self.returncode = returncode
        self.responses = responses
        self.stdout = stdout
        self.stderr = stderr

    def response_by_id(self, req_id):
        for r in self.responses:
            if isinstance(r, dict) and r.get("id") == req_id:
                return r
        return None

    def tool_names(self):
        for r in self.responses:
            if not isinstance(r, dict):
                continue
            result = r.get("result")
            if not isinstance(result, dict):
                continue
            tools = result.get("tools")
            if isinstance(tools, list):
                return [t["name"] for t in tools if isinstance(t, dict) and "name" in t]
        return []


def run_mcp(flags, requests, timeout=30, env=None):
    stdin_text = "\n".join(json.dumps(r) for r in requests) + "\n"
    try:
        proc = subprocess.run(
            [str(BIN), "--mcp", *flags],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=REPO_ROOT,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return Mcp(returncode=-1, responses=[], stdout=exc.stdout or "", stderr=str(exc))

    responses = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return Mcp(returncode=proc.returncode, responses=responses, stdout=proc.stdout, stderr=proc.stderr)


def initialize_request(req_id=1, client_info=None):
    params = {}
    if client_info is not None:
        params["clientInfo"] = client_info
    return {"jsonrpc": "2.0", "id": req_id, "method": "initialize", "params": params}


def tools_list_request(req_id=2):
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}}


def tools_call_request(req_id, name, arguments):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def tool_result_text(response):
    return response["result"]["content"][0]["text"]


def tool_is_error(response):
    return response["result"].get("isError", False)
