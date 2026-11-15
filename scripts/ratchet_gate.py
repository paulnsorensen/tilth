#!/usr/bin/env python3
"""Check every metric in perf/ratchet/*.json against its threshold.

Usage: scripts/ratchet_gate.py [ratchet-dir]

Each metric stores the command that measures it. The command prints the
value on its last stdout line. A regression or a failed command fails the
gate. Thresholds move only through `scripts/ratchet.py tighten`.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RATCHET = REPO_ROOT / "scripts" / "ratchet.py"


def main() -> int:
    failures = 0
    ratchet_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "perf" / "ratchet"
    for ratchet_file in sorted(ratchet_dir.glob("*.json")):
        for name, entry in json.loads(ratchet_file.read_text())["metrics"].items():
            command = entry.get("command")
            if not command:
                print(f"{ratchet_file.name}: {name} has no stored command", file=sys.stderr)
                failures += 1
                continue
            run = subprocess.run(
                ["bash", "-c", command], cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
            )
            lines = run.stdout.strip().splitlines()
            if run.returncode != 0 or not lines:
                print(f"{ratchet_file.name}: {name}: `{command}` failed\n{run.stderr}", file=sys.stderr)
                failures += 1
                continue
            check = subprocess.run([
                sys.executable, str(RATCHET), "check", "--partial",
                "--file", str(ratchet_file), "--value", f"{name}={lines[-1]}",
            ])
            failures += check.returncode != 0
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
