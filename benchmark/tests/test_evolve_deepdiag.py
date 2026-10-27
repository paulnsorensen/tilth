'''Tests for deep diagnosis: one Codex run per parent, fed neutral dev-only copies, answering scrubbed causes.

No test calls codex: the runner is a stub that writes the answer file named in its argv.
'''

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import candidate as candidates
from evolve import deepdiag, diagnosis, loop
from evolve_support import DEV, TEST, build, seed_candidate
from test_evolve import world  # noqa: F401  (world is a fixture)

CAUSE = {'cause': 'extra read after search', 'evidence': 'task 0 turn 4', 'component_hint': 'src_patch',
         'suggested_change': 'return the body in the search hit', 'expected_calls_saved': 1.5}


def answer_of(*causes: dict) -> str:
    return 'Turn by turn comparison.\n```json\n' + json.dumps(list(causes)) + '\n```\n'


class Stub:
    '''A runner that records each argv and answers like codex would.'''

    def __init__(self, answer: str = '', code: int = 0, error: Exception | None = None) -> None:
        self.answer, self.code, self.error = answer or answer_of(CAUSE), code, error
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], timeout: float) -> int:
        self.calls.append((argv, timeout))
        if self.error:
            raise self.error
        Path(argv[argv.index('-o') + 1]).write_text(self.answer)
        return self.code


def row(task: str, rep: int = 0, *, path: Path | None = None, **extra) -> dict:
    return {'task': task, 'repetition': rep, 'correct': True, 'total_cost_usd': 0.5, 'api_calls': 7,
            'trajectory_path': str(path) if path else None, **extra}


def make(tmp_path: Path, runner, logs: list[str] | None = None, *, forbidden=('secret_task',), **kwargs):
    return deepdiag.DeepDiagnosis(tmp_path / 'deep', dev_tasks={'dev_a', 'dev_b'}, forbidden=forbidden,
                                  log=(logs if logs is not None else []).append, runner=runner, **kwargs)


# --- the codex call ---


def test_the_command_names_the_model_effort_sandbox_and_scratch_dir(tmp_path) -> None:
    stub = Stub()
    deep = make(tmp_path, stub, model='m-1', effort='high', timeout=42)

    deep.records('c' * 64, [row('dev_a')], [row('dev_a')])

    argv, timeout = stub.calls[0]
    scratch = tmp_path / 'deep' / ('c' * 12)
    assert argv[:11] == ['codex', 'exec', '-m', 'm-1', '-c', 'model_reasoning_effort=high', '-s', 'read-only',
                         '--skip-git-repo-check', '-C', str(scratch)]
    assert argv[11:13] == ['-o', str(scratch / 'answer.txt')]
    assert argv[13] == deepdiag.PROMPT
    assert timeout == 42


def test_defaults_match_the_spec() -> None:
    assert (deepdiag.DEFAULT_MODEL, deepdiag.DEFAULT_EFFORT) == ('gpt-6-astra', 'xhigh')
    assert (deepdiag.DEFAULT_MAX_RUNS, deepdiag.DEFAULT_TIMEOUT_S) == (10, 1800)


# --- the scratch directory ---


def test_scratch_holds_only_dev_trajectories_under_neutral_names(tmp_path) -> None:
    sources = tmp_path / 'src'
    sources.mkdir()
    for name in ('t0', 't1', 'b0', 'x0'):
        (sources / f'{name}.jsonl').write_text(name)
    stub = Stub()
    deep = make(tmp_path, stub)
    tilth = [row('dev_b', path=sources / 't0.jsonl'), row('dev_a', 1, path=sources / 't1.jsonl'),
             row('secret_task', path=sources / 'x0.jsonl')]
    baseline = [row('dev_b', path=sources / 'b0.jsonl', api_calls=None, per_turn_context_tokens=[1, 2, 3])]

    deep.records('d' * 64, tilth, baseline)

    scratch = tmp_path / 'deep' / ('d' * 12)
    assert sorted(p.name for p in scratch.iterdir()) == [
        'answer.txt', 'summary.json', 'task-0-tilth-rep1.jsonl', 'task-1-baseline-rep0.jsonl',
        'task-1-tilth-rep0.jsonl']
    assert (scratch / 'task-1-tilth-rep0.jsonl').read_text() == 't0'
    summary = json.loads((scratch / 'summary.json').read_text())
    assert {tuple(item[k] for k in ('task', 'arm', 'rep', 'api_calls')) for item in summary} == {
        (0, 'tilth', 1, 7), (1, 'tilth', 0, 7), (1, 'baseline', 0, 3)}
    text = ' '.join(p.name for p in scratch.iterdir()) + (scratch / 'summary.json').read_text()
    assert not any(term in text for term in ('secret_task', 'dev_a', 'dev_b', 'x0'))
    assert set(summary[0]) == {'task', 'arm', 'rep', 'correct', 'cost', 'api_calls'}


# --- memoization and the cap ---


def test_one_run_per_parent_and_the_record_is_memoized(tmp_path) -> None:
    stub = Stub()
    deep = make(tmp_path, stub)

    first = deep.records('a' * 64, [row('dev_a')], [])
    again = deep.records('a' * 64, [row('dev_a')], [])
    other = deep.records('b' * 64, [row('dev_a')], [])

    assert first == again == other
    assert len(stub.calls) == 2
    assert first[0]['deep_diagnosis'][0]['cause'] == CAUSE['cause']


def test_the_maximum_stops_further_runs_and_logs_it(tmp_path) -> None:
    stub, logs = Stub(), []
    deep = make(tmp_path, stub, logs, max_runs=1)

    assert deep.records('a' * 64, [row('dev_a')], [])
    assert deep.records('b' * 64, [row('dev_a')], []) == []

    assert len(stub.calls) == 1
    assert any('1-run maximum is spent' in line for line in logs)


# --- scrubbing ---


def test_scrub_drops_entries_with_forbidden_terms_and_redacts_absolute_paths() -> None:
    leak = {**CAUSE, 'cause': 'the secret_task run reads too much'}
    pathy = {**CAUSE, 'suggested_change': 'edit /home/paul/Dev/tilth/src/search/mod.rs and src/read/mod.rs'}
    extra_key = {**CAUSE, 'grader_output': 'expected 42'}

    kept = deepdiag.scrub([leak, pathy, extra_key], ['secret_task'])

    assert len(kept) == 2
    assert kept[0]['suggested_change'] == 'edit <path> and src/read/mod.rs'
    assert 'grader_output' not in kept[1]
    assert 'home/paul' not in json.dumps(kept)


def test_scrub_keeps_at_most_five_causes() -> None:
    causes = [{**CAUSE, 'cause': f'cause {i}'} for i in range(9)]

    assert [c['cause'] for c in deepdiag.scrub(causes, [])] == [f'cause {i}' for i in range(5)]


def test_a_leaking_answer_never_reaches_the_record(tmp_path) -> None:
    leaky = {**CAUSE, 'evidence': 'see /tmp/x/secret_task.json'}
    deep = make(tmp_path, Stub(answer_of(leaky, CAUSE)))

    records = deep.records('a' * 64, [row('dev_a')], [])

    assert [c['cause'] for c in records[0]['deep_diagnosis']] == [CAUSE['cause']]


def test_parse_causes_takes_the_last_json_array() -> None:
    text = 'draft [1, 2]\n```json\n[{"cause": "x"}]\n```\n'

    assert deepdiag.parse_causes(text) == [{'cause': 'x'}]
    assert deepdiag.parse_causes('no json here') is None


# --- failure tolerance ---


@pytest.mark.parametrize('stub, message', [
    (Stub(error=subprocess.TimeoutExpired(['codex'], 1)), 'timed out'),
    (Stub(code=3), 'codex exited 3'),
    (Stub(error=OSError('no codex')), 'no codex'),
    (Stub(answer='I could not find a pattern.'), 'no JSON array'),
])
def test_a_failed_run_costs_only_the_record_and_is_not_retried(tmp_path, stub, message) -> None:
    logs: list[str] = []
    deep = make(tmp_path, stub, logs)

    assert deep.records('a' * 64, [row('dev_a')], []) == []
    assert deep.records('a' * 64, [row('dev_a')], []) == []

    assert len(stub.calls) == 1
    assert any(message in line and 'continuing without it' in line for line in logs)


def test_the_wall_time_is_logged_per_run(tmp_path) -> None:
    logs: list[str] = []
    ticks = iter([0.0, 12.5, 12.5, 12.5])
    deep = make(tmp_path, Stub(), logs, clock=lambda: next(ticks))

    deep.records('a' * 64, [row('dev_a')], [])

    assert any('wall 12.5s' in line for line in logs)


# --- wiring ---


def parent_with_rows(evo, rows: list[dict]) -> dict:
    parent = seed_candidate()
    cid = candidates.content_id(parent)
    evo.results[cid] = loop.Cascade(cid, 1, parent, rows=rows)
    return parent


def rows_for(*tasks: str) -> list[dict]:
    return [{'task': task, 'repetition': 0, 'contaminated': False, 'correct': True, 'total_cost_usd': 0.1,
             'api_calls': 4} for task in tasks]


def test_deep_diagnosis_is_off_by_default(world) -> None:  # noqa: F811
    evo = build(world)

    assert evo.deep is None
    assert (evo.settings.deep_diagnosis_model, evo.settings.deep_diagnosis_effort) == ('gpt-6-astra', 'xhigh')
    assert (evo.settings.deep_diagnosis_max, evo.settings.deep_diagnosis_timeout) == (10, 1800)
    assert not any('deep_diagnosis' in str(record) for record in evo._extra_records(seed_candidate()))


def test_the_flag_adds_one_record_for_the_parent_from_dev_rows_only(world) -> None:  # noqa: F811
    stub = Stub()
    evo = build(world, '--deep-diagnosis', '--deep-diagnosis-model', 'm-2', deep_runner=stub)
    evo.dev_baseline_rows = rows_for(DEV[0])
    parent = parent_with_rows(evo, rows_for(DEV[0], TEST[0]))

    first = evo._extra_records(parent)
    again = evo._extra_records(parent)

    assert [r for r in first if 'deep_diagnosis' in r] == [{'deep_diagnosis': [CAUSE]}]
    assert first == again
    assert len(stub.calls) == 1
    assert stub.calls[0][0][3] == 'm-2'
    scratch = Path(stub.calls[0][0][stub.calls[0][0].index('-C') + 1])
    assert TEST[0] not in (scratch / 'summary.json').read_text()
    assert json.loads((scratch / 'summary.json').read_text())
    assert not scratch.is_relative_to(evo.run_dir), 'the -C path must not sit under the run dir'
    assert not any(term and term in str(scratch) for term in evo._forbidden_terms()), scratch


def test_a_parent_without_dev_rows_skips_the_run_and_says_so(world) -> None:  # noqa: F811
    stub = Stub()
    evo = build(world, '--deep-diagnosis', deep_runner=stub)
    parent = parent_with_rows(evo, [])

    assert not any('deep_diagnosis' in r for r in evo._extra_records(parent))
    assert stub.calls == []


def test_the_note_tells_the_model_about_the_deep_record() -> None:
    assert 'deep_diagnosis' in diagnosis.DIAGNOSIS_NOTE


@pytest.mark.parametrize('flag, value', [('--deep-diagnosis-max', '-1'), ('--deep-diagnosis-timeout', '0')])
def test_build_refuses_out_of_range_deep_options(world, flag, value, capsys) -> None:  # noqa: F811
    assert build(world, '--deep-diagnosis', flag, value) == 2
    assert flag.lstrip('-') in capsys.readouterr().err
