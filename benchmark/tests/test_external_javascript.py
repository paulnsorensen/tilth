"""JavaScript runners for native external tasks: command rewrites, report parsing, config resets."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from external import data, javascript, preflight, swebench_ml, task

MOCHA = "(npx mocha test/unit/adapters/http.js -R tap -g 'FormData') | cat"
QUNIT = "(npx qunit test/unit/src/math/Sphere.tests.js) | cat"
VITEST = '(pnpm run test packages/x/hydration.spec.ts --no-watch --reporter=verbose -t "mismatch") | cat'
JEST = "(yarn test packages/theme/options.test.ts --verbose) | cat"
REPORT = Path("/r/report")
REPORTER = Path("/r/reporter.cjs")


@pytest.mark.parametrize("command, runner", [(MOCHA, "mocha"), (QUNIT, "qunit"), (VITEST, "vitest"), (JEST, "jest")])
def test_runner_of_names_the_runner(command, runner):
    assert javascript.runner_of(command) == runner


def test_runner_of_refuses_karma_and_unknown_commands():
    karma = "COVERAGE=false npx karma start karma.conf.js --single-run"
    with pytest.raises(javascript.UnsupportedRunner, match="browser"):
        javascript.runner_of(karma)
    with pytest.raises(javascript.UnsupportedRunner):
        javascript.runner_of("make test")


def test_rewrite_points_each_runner_at_a_report_file():
    mocha, env = javascript.rewrite(MOCHA, "mocha", REPORT, REPORTER)
    assert "-R tap" not in mocha and f"--reporter json --reporter-option output={REPORT}" in mocha and env == {}
    qunit, env = javascript.rewrite(QUNIT, "qunit", REPORT, REPORTER)
    assert qunit.startswith(f"(npx qunit --require {REPORTER} test/unit") and env == {"TILTH_QUNIT_REPORT": str(REPORT)}
    vitest, _ = javascript.rewrite(VITEST, "vitest", REPORT, REPORTER)
    assert "--reporter=verbose" not in vitest and f"--reporter=json --outputFile={REPORT}" in vitest
    jest, _ = javascript.rewrite(JEST, "jest", REPORT, REPORTER)
    assert "--verbose" not in jest and f"--json --outputFile={REPORT}" in jest


def test_mocha_report_maps_passes_and_failures_by_full_title():
    raw = json.dumps({"passes": [{"fullTitle": "a b"}], "failures": [{"fullTitle": "a c"}], "pending": [{"fullTitle": "a d"}]})
    assert json.loads(javascript.normalize("mocha", raw, Path("/w"))) == [["a b", True], ["a c", False]]


def test_qunit_report_reads_one_json_line_per_test():
    raw = '{"id": "Maths > Sphere > set", "passed": true}\n{"id": "Maths > Sphere > copy", "passed": false}\n'
    assert json.loads(javascript.normalize("qunit", raw, Path("/w"))) == [
        ["Maths > Sphere > set", True], ["Maths > Sphere > copy", False]]


def test_vitest_report_ids_carry_the_relative_file_and_describe_path():
    raw = json.dumps({"testResults": [{"name": "/w/packages/x/hydration.spec.ts", "assertionResults": [
        {"ancestorTitles": ["SSR", "mismatch"], "title": "escape", "status": "passed"},
        {"ancestorTitles": ["SSR"], "title": "skipped", "status": "pending"},
    ]}]})
    assert json.loads(javascript.normalize("vitest", raw, Path("/w"))) == [
        ["packages/x/hydration.spec.ts > SSR > mismatch > escape", True]]


def test_jest_report_ids_are_the_bare_title():
    raw = json.dumps({"testResults": [{"name": "/w/a.test.ts", "assertionResults": [
        {"title": "accepts valid config", "status": "passed"}, {"title": "rejects", "status": "failed"}]}]})
    assert json.loads(javascript.normalize("jest", raw, Path("/w"))) == [
        ["accepts valid config", True], ["rejects", False]]


def test_entry_needs_exactly_one_passing_verdict():
    report = json.dumps([["one", True], ["dup", True], ["dup", True], ["mixed", True], ["mixed", False]])
    verdicts = swebench_ml.entry_verdicts(report, ["one", "dup", "mixed", "absent"], "javascript")
    assert verdicts == {"one": True, "dup": False, "mixed": False, "absent": False}


def test_log_text_cannot_forge_a_verdict():
    assert swebench_ml.entry_verdicts(json.dumps([]), ["t"], "javascript") == {"t": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_qunit_reporter_appends_one_line_per_finished_test(tmp_path):
    reporter, report = tmp_path / "reporter.cjs", tmp_path / "report"
    reporter.write_text(javascript.QUNIT_REPORTER)
    script = ("let handler; globalThis.QUnit = {on: (_, fn) => { handler = fn; }};"
              f"require({json.dumps(str(reporter))});"
              "handler({fullName: ['M', 'a'], status: 'passed'}); handler({fullName: ['M', 'b'], status: 'failed'});")
    subprocess.run(["node", "-e", script], check=True, env={"TILTH_QUNIT_REPORT": str(report), "PATH": "/usr/bin:/bin"})
    assert json.loads(javascript.normalize("qunit", report.read_text(), tmp_path)) == [["M > a", True], ["M > b", False]]


def test_package_json_reset_keeps_dependency_edits_and_restores_scripts():
    base = json.dumps({"scripts": {"test": "jest"}, "jest": {"a": 1}, "dependencies": {"x": "1"}}).encode()
    edited = json.dumps({"scripts": {"test": "true"}, "dependencies": {"x": "1", "y": "2"}}).encode()
    result = json.loads(javascript.reset_package_json(edited, base))
    assert result == {"scripts": {"test": "jest"}, "jest": {"a": 1}, "dependencies": {"x": "1", "y": "2"}}
    assert javascript.reset_package_json(base, base) == base
    assert javascript.reset_package_json(b"{broken", base) == base
    assert javascript.reset_package_json(edited, None) is None


def test_config_files_are_recognised():
    for name in ("jest.config.js", "vitest.config.ts", ".mocharc.json", ".npmrc", "tsconfig.json", "babel.config.cjs"):
        assert javascript.is_config(name)
    assert not javascript.is_config("index.js")


def test_install_follows_the_lockfile(tmp_path):
    assert javascript.install_argv(tmp_path, True)[0] == "npm"
    (tmp_path / "yarn.lock").write_text("")
    assert javascript.install_argv(tmp_path, True)[:3] == ["yarn", "install", "--frozen-lockfile"]
    assert "--frozen-lockfile" not in javascript.install_argv(tmp_path, False)
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert javascript.install_argv(tmp_path, True)[0] == "pnpm"
    assert "--ignore-scripts" in javascript.install_argv(tmp_path, True)


def test_build_script_runs_through_the_lockfile_manager(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build:packages": "lerna run build"}}))
    (tmp_path / "yarn.lock").write_text("")
    assert javascript.build_argv(tmp_path) == ["yarn", "run", "build:packages"]
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    assert javascript.build_argv(tmp_path) is None


def test_module_dirs_and_built_paths_find_installed_and_generated_files(tmp_path):
    for relative in ("src/a.js", "node_modules/dep/index.js", "pkg/node_modules/dep/i.js", "pkg/lib/out.js",
                     "pkg/src/b.js", "dist/bundle.js", "new.txt"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("x")
    base = frozenset({"src/a.js", "pkg/src/b.js"})
    assert javascript.module_dirs(tmp_path) == ["node_modules", "pkg/node_modules"]
    assert javascript.built_paths(tmp_path, base) == ["dist", "new.txt", "pkg/lib"]


def test_grading_walk_ignores_node_modules(tmp_path):
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "index.js").write_text("x")
    (tmp_path / "index.js").write_text("x")
    assert task._tree_files(tmp_path) == {"index.js"}
    assert "node_modules/" in task.EXCLUDED_PATHS


def test_javascript_repositories_map_to_the_javascript_language():
    assert "javascript" in data.LANGUAGES
    for repo in ("axios/axios", "mrdoob/three.js", "vuejs/core", "facebook/docusaurus"):
        assert data.language_of({"repo": repo}) == "javascript"
    assert data.language_of({"repo": "prometheus/prometheus"}) == "go"


def test_dotted_swebench_ids_are_not_featurebench_ids():
    assert preflight.featurebench.level_of("mrdoob__three.js-27395") is None
    assert preflight.featurebench.level_of("pandas-dev__pandas.abc12345.lv1") == "lv1"


def test_candidates_include_the_admitted_javascript_ids_and_not_preact():
    assert "axios__axios-5316" in swebench_ml.GEPA_V2_CANDIDATES
    assert "preactjs__preact-3689" not in swebench_ml.GEPA_V2_CANDIDATES
