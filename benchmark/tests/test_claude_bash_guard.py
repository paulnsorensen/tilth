"""The strict Claude Bash hook allows only Go checks and gofmt writes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).parents[1] / "claude_bash_guard.py"


@pytest.mark.parametrize("command", [
    "go test -run '^(TestContext|TestMiddleware|TestHeldOut)' .",
    "go test -run '^$' ./...",
    "go test -tags nomsgpack -count=1 -timeout 2m ./...",
    "go test -tags=nomsgpack -count 1 ./...",
    "go build -tags=nomsgpack ./...",
    "go vet -tags nomsgpack .",
    "go build ./...",
    "go vet .",
    "gofmt -w render/render.go context.go",
])
def test_guard_allows_go_checks_and_formatting(command: str) -> None:
    result = subprocess.run([sys.executable, str(GUARD)],
                            input=json.dumps({"tool_input": {"command": command}}),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", [
    "cat context.go", "rg Render", "python3 -c 'open(\"context.go\").read()'",
    "sh -c 'go test .'", "go run context.go", "go generate ./...",
    "go test -exec /tmp/tool .", "go test -toolexec=cat .",
    "go test .; cat context.go", "go test . | cat",
    "go test $(cat secret) .", "go test . > output",
    "VAR=1 go test .", "go test ../...", "gofmt -w ../secret.go",
    "go test -run \"^$\" .", "go test -run '^Test' . && cat secret",
])
def test_guard_denies_file_access_and_shell_composition(command: str) -> None:
    result = subprocess.run([sys.executable, str(GUARD)],
                            input=json.dumps({"tool_input": {"command": command}}),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "Only go test, go build, go vet, and gofmt -w" in result.stderr


def test_guard_denies_malformed_hook_input() -> None:
    result = subprocess.run([sys.executable, str(GUARD)], input="{}",
                            capture_output=True, text=True)
    assert result.returncode == 2
