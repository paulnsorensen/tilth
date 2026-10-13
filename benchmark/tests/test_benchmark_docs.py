"""Documented run.py invocations must pass the run-wide spend ceiling run.py requires."""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = (".claude/skills/tilth-benchmark/SKILL.md", "CLAUDE.md")


def _run_commands(markdown: str) -> list[str]:
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", markdown, flags=re.DOTALL):
        for command in block.replace("\\\n", " ").splitlines():
            if re.match(r"\s*python3? benchmark/run\.py\b", command):
                commands.append(command)
    return commands


@pytest.mark.parametrize("doc", _DOCS)
def test_documented_run_commands_pass_max_usd(doc: str) -> None:
    commands = _run_commands((_ROOT / doc).read_text())

    assert commands, f"{doc} has no run.py command"
    assert [command for command in commands if "--max-usd" not in command] == []
