"""Fail-closed Bash policy for strict Claude benchmark cells."""

import json
import re
import shlex
import sys

_GUIDANCE = "Only go test, go build, go vet, and gofmt -w on local Go files are allowed."
_LOCAL_PATH = re.compile(r"\.?/?[A-Za-z0-9_./-]+\Z")
_DURATION = re.compile(r"[0-9]+(?:ns|us|ms|s|m|h)\Z")
_TAGS = re.compile(r"[A-Za-z0-9_,]+\Z")


def _shell_safe(command: str) -> bool:
    quote = ""
    for char in command:
        if char == "\\" or char in "\r\n\x00":
            return False
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char in "$`":
                return False
        elif char in "'\"":
            quote = char
        elif char in ";&|<>`$()#*?[]{}":
            return False
    return not quote


def _local_path(value: str, *, go_file: bool = False) -> bool:
    if value in {".", "./..."} and not go_file:
        return True
    if not _LOCAL_PATH.fullmatch(value) or value.startswith(("/", "-")):
        return False
    parts = value.removeprefix("./").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if go_file:
        return value.endswith(".go") and len(value) > 3
    return value.startswith("./")


def allowed_command(command: str) -> bool:
    if not _shell_safe(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    if tokens[0] == "gofmt":
        return len(tokens) >= 3 and tokens[1] == "-w" and all(
            _local_path(path, go_file=True) for path in tokens[2:]
        )
    if len(tokens) < 3 or tokens[0] != "go" or tokens[1] not in {"test", "build", "vet"}:
        return False
    operation = tokens[1]
    index = 2
    while index < len(tokens) and tokens[index].startswith("-"):
        flag = tokens[index]
        if operation == "test" and flag == "-run" and index + 1 < len(tokens):
            index += 2
        elif flag == "-tags" and index + 1 < len(tokens) and _TAGS.fullmatch(tokens[index + 1]):
            index += 2
        elif re.fullmatch(r"-tags=[A-Za-z0-9_,]+", flag):
            index += 1
        elif operation == "test" and flag == "-timeout" and index + 1 < len(tokens) and _DURATION.fullmatch(tokens[index + 1]):
            index += 2
        elif operation == "test" and flag == "-count" and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            index += 2
        elif operation == "test" and re.fullmatch(r"-count=[0-9]+", flag):
            index += 1
        elif operation == "test" and re.fullmatch(r"-timeout=[0-9]+(?:ns|us|ms|s|m|h)", flag):
            index += 1
        elif operation == "test" and flag in {"-v", "-race"}:
            index += 1
        else:
            return False
    return index < len(tokens) and all(_local_path(path) for path in tokens[index:])


def main() -> int:
    try:
        event = json.load(sys.stdin)
        command = event["tool_input"]["command"]
    except (ValueError, KeyError, TypeError):
        command = None
    if isinstance(command, str) and allowed_command(command):
        return 0
    print(_GUIDANCE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
