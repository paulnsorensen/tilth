#!/usr/bin/env python3
"""Print the byte size of the fixed MCP prefix that tilth adds to every API call.

The prefix is the server instructions plus the served tool list (name,
description, input schema). A host sends both on every model call, so each
byte here is paid once per call. The last stdout line is the total, for
the hill-climb ratchet in perf/ratchet/.

Usage: scripts/mcp_prefix_bytes.py [path/to/tilth]
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    binary = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "target" / "debug" / "tilth"
    if len(sys.argv) == 1 and not binary.exists():
        subprocess.run(["cargo", "build", "--bin", "tilth"], cwd=REPO_ROOT, check=True)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.run(
        [str(binary), "--mcp"],
        input="\n".join(json.dumps(request) for request in requests) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        check=True,
    )
    responses = {}
    for line in proc.stdout.splitlines():
        if line.strip():
            message = json.loads(line)
            responses[message.get("id")] = message
    instructions = responses[1]["result"]["instructions"]
    tools = responses[2]["result"]["tools"]
    if not instructions or not tools:
        print("tilth served no instructions or no tools", file=sys.stderr)
        return 1
    sizes = {"instructions": len(instructions.encode())}
    for tool in tools:
        sizes[f"{tool['name']}.description"] = len(tool["description"].encode())
        schema = json.dumps(tool["inputSchema"], separators=(",", ":"), sort_keys=True)
        sizes[f"{tool['name']}.schema"] = len(schema.encode())
    for name, size in sizes.items():
        print(f"{name}\t{size}")
    print(sum(sizes.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
