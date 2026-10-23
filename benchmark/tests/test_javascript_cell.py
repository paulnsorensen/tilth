"""JavaScript cells: the agent runs on the harness Node, and its npm fetches of the task package are flagged."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import external.contamination as contamination
import external.javascript
import run

AMBIENT = {"PATH": "/usr/bin:/bin"}


def agent_env(language: str | None, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(external.javascript, "node_bin", lambda: Path("/data/node/v20/bin"))
    return run.build_runner_env("claude", ambient=AMBIENT, tilth_bin=None, language=language)


def test_javascript_agent_gets_the_harness_node_first(monkeypatch):
    env = agent_env("javascript", monkeypatch)
    assert env["PATH"] == "/data/node/v20/bin:/usr/bin:/bin"


@pytest.mark.parametrize("language", [None, "python", "go", "rust"])
def test_other_languages_keep_their_agent_env(language, monkeypatch):
    env = agent_env(language, monkeypatch)
    assert env["PATH"] == "/usr/bin:/bin"


def rules(package: str) -> contamination._Rules:
    return contamination._Rules((), (), (), (), None, (), None, npm_package=package)


def flagged(text: str, package: str = "axios") -> bool:
    return "package_source" in contamination._call_reasons({"command": text}, rules(package))


@pytest.mark.parametrize("command", [
    "npm install axios@1.6.0",
    "npm i axios",
    "pnpm add -D axios@latest",
    "yarn add axios",
    "npm view axios version",
    "npm info axios dist.tarball",
    "npm pack axios@1.7.0 && tar xf axios-1.7.0.tgz",
    "npx axios",
    "curl https://registry.npmjs.org/axios/-/axios-1.7.0.tgz",
    "curl -s https://unpkg.com/axios@1.7.0/lib/adapters/http.js",
    "wget https://cdn.jsdelivr.net/npm/axios@1.7.0/index.js",
])
def test_npm_fetch_of_the_task_package_is_flagged(command):
    assert flagged(command)


@pytest.mark.parametrize("command", [
    "npm install",
    "npm install --ignore-scripts",
    "npm install lodash",
    "npm view lodash version",
    "npm test -- --grep axios",
    "npx mocha test/unit/adapters/http.js",
    "yarn add axios-mock-adapter",
    "curl https://registry.npmjs.org/axios-retry",
    "cat node_modules/axios/package.json",
])
def test_other_npm_commands_are_clean(command):
    assert not flagged(command)


def test_scoped_package_names_are_matched():
    assert flagged("npm install @scope/pkg@2", "@scope/pkg")
    assert flagged("curl https://registry.npmjs.org/@scope%2fpkg", "@scope/pkg")
    assert not flagged("npm install @scope/other", "@scope/pkg")


def test_npm_rule_is_off_without_a_javascript_task():
    assert "package_source" not in contamination._call_reasons({"command": "npm install axios"}, rules(None))
