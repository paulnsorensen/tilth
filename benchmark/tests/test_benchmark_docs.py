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


_WIKI = _ROOT / ".hallouminate" / "wiki"
_FEATUREBENCH_PAGE = _WIKI / "featurebench-integration.md"
_ADR_LINK = "adr/benchmark-gepa-overhaul.md"
_PHASE4_CLAIM = re.compile(r"PHASE4_PLAN|Phase 4 (power )?gate|power[- ]gate", re.IGNORECASE)
_RETIRED_WORD = re.compile(r"(?<!not )(?<!never )(?<!n't )\b(retired|deleted|removed|superseded)\b", re.IGNORECASE)


def _rejected_alternatives(markdown: str) -> str:
    match = re.search(r"^## Rejected alternatives\n(.*?)(?=^## |^_Source:|\Z)", markdown, flags=re.DOTALL | re.MULTILINE)
    assert match, "featurebench-integration.md has no 'Rejected alternatives' section"
    return match.group(1)


def _is_live_gate_claim(line: str) -> bool:
    return bool(_PHASE4_CLAIM.search(line)) and not _RETIRED_WORD.search(line)


@pytest.mark.parametrize(
    ("line", "live"),
    [
        ("The Phase 4 power gate blocks runs.", True),
        ("The power gate was not removed and still blocks runs.", True),
        ("The power gate was never removed.", True),
        ("The Phase 4 gate was removed.", False),
        ("PHASE4_PLAN is retired.", False),
        ("Phase 4 of the roadmap ships next month.", False),
    ],
)
def test_phase4_claim_detector_rejects_live_gate_lines(line: str, live: bool) -> None:
    assert _is_live_gate_claim(line) is live


def test_no_current_page_presents_the_phase4_gate_as_live() -> None:
    pages = [path for path in _WIKI.rglob("*.md") if path.name != "log.md" and "adr" not in path.relative_to(_WIKI).parts]

    assert pages
    live = [
        f"{path.relative_to(_WIKI)}:{number}: {line.strip()}"
        for path in pages
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if _is_live_gate_claim(line)
    ]
    assert live == []


def test_featurebench_page_names_harbor_and_docker_only_as_rejected() -> None:
    markdown = _FEATUREBENCH_PAGE.read_text()
    section = _rejected_alternatives(markdown)
    outside = markdown.replace(section, "")

    assert re.search(r"harbor", section, re.IGNORECASE)
    assert re.search(r"docker", section, re.IGNORECASE)
    assert re.search(r"harbor|docker", outside, re.IGNORECASE) is None


def test_rejected_alternatives_link_the_gepa_overhaul_adr() -> None:
    assert _ADR_LINK in _rejected_alternatives(_FEATUREBENCH_PAGE.read_text())
