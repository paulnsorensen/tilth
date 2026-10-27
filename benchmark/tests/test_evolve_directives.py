'''Tests for evolve directives: a queue of seeded proposals that the selector and dispatcher honour first.

No test calls claude: the proposer and the reflection are lambdas.
'''

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evolve import candidate as candidates
from evolve import diagnosis, directives, engine, proposer
from evolve_support import build, log_text, seed_candidate
from test_evolve import world  # noqa: F401  (world is a fixture)

SRC = candidates.SRC_PATCH
READ_MD = 'prompts/tools/read.md'
MCP_MD = 'prompts/mcp.md'
COMPONENTS = tuple(seed_candidate())
SHIPPED = Path(__file__).resolve().parents[1] / 'evolve' / 'directives' / 'gepa-v3-astra.json'
HYPOTHESIS = {'metric': 'calls_per_cell', 'scope': 'tilth_search', 'direction': 'decrease', 'tolerance': 0.1,
              'invariants': ['accuracy does not drop']}


def entry(id_: str, component: str = SRC, **extra) -> dict:
    return {'id': id_, 'component': component, 'brief': f'change {id_}', **extra}


def write_file(tmp_path: Path, content) -> Path:
    path = tmp_path / 'directives.json'
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def queue_of(*items: dict, logs: list[str] | None = None) -> directives.DirectiveQueue:
    parsed = [directives.Directive(i['id'], i['component'], i['brief'], i.get('hypothesis')) for i in items]
    return directives.DirectiveQueue(parsed, (logs if logs is not None else []).append)


def paid_entry() -> dict:
    return {'records': [{'kind': 'fixture'}]}


def hypothesis_text(metric: str) -> str:
    return f'<hypothesis>{json.dumps({**HYPOTHESIS, "metric": metric})}</hypothesis>'


# --- validation ---


def test_load_returns_directives_in_file_order_with_a_parsed_hypothesis(tmp_path) -> None:
    path = write_file(tmp_path, [entry('b', READ_MD), entry('a', SRC, hypothesis=HYPOTHESIS)])

    loaded = directives.load(path, COMPONENTS)

    assert [d.id for d in loaded] == ['b', 'a']
    assert loaded[0].hypothesis is None
    assert loaded[1].hypothesis["metric"] == 'calls_per_cell'
    assert loaded[1].record() == {'directive': {'id': 'a', 'brief': 'change a'}}


@pytest.mark.parametrize('content, message', [
    ('{not json', 'cannot read'),
    ({'id': 'a'}, 'must be a JSON list'),
    (['text'], 'not an object'),
    ([{**entry('a'), 'extra': 1}], 'unknown keys: extra'),
    ([{**entry('a'), 'brief': '  '}], 'non-empty string brief'),
    ([{k: v for k, v in entry('a').items() if k != 'id'}], 'non-empty string id'),
    ([entry('a', 'prompts/missing.md')], 'prompts/missing.md'),
    ([entry('a'), entry('a', READ_MD)], 'more than once'),
    ([entry('a', hypothesis={'metric': 'nonsense'})], 'directive'),
])
def test_load_refuses_a_bad_file_with_a_clear_error(tmp_path, content, message) -> None:
    with pytest.raises(directives.DirectiveError, match=message):
        directives.load(write_file(tmp_path, content), COMPONENTS)


def test_load_refuses_a_brief_that_names_a_forbidden_term(tmp_path) -> None:
    path = write_file(tmp_path, [{**entry('a'), 'brief': 'look at the secret_task trajectory'}])

    with pytest.raises(directives.DirectiveError, match='secret_task'):
        directives.load(path, COMPONENTS, ['secret_task'])


def test_the_shipped_file_loads_and_never_points_the_proposer_at_the_benchmark() -> None:
    loaded = directives.load(SHIPPED, COMPONENTS)

    assert len(loaded) == 5
    assert all(d.hypothesis for d in loaded)
    for d in loaded:
        assert 'benchmark' not in d.brief.lower()
        assert 'panel' not in d.brief.lower()


# --- queue and selector ---


def test_queue_hands_out_the_head_once_per_take_and_logs_it() -> None:
    logs: list[str] = []
    queue = queue_of(entry('a', READ_MD), entry('b'), logs=logs)

    assert queue.take(1).id == 'a'
    assert queue.claim().id == 'a'
    assert queue.claim() is None
    assert queue.take(2).id == 'b'
    assert [d.id for d in queue.pending] == []
    assert logs == ['directive: a -> proposal 1 (prompts/tools/read.md)', 'directive: b -> proposal 2 (src_patch)']


def test_selector_follows_the_directive_order_and_logs_each_override() -> None:
    logs: list[str] = []
    selector = diagnosis.WeightedSelector(lambda candidate: None, logs.append, random.Random(0),
                                          directives=queue_of(entry('a', READ_MD), entry('b', MCP_MD), logs=logs))
    candidate = seed_candidate()

    picks = [selector(None, None, None, 0, candidate) for _ in range(2)]

    assert picks == [[READ_MD], [MCP_MD]]
    assert sum('directive override' in line for line in logs) == 2
    assert 'directive override a' in logs[1]


def test_selector_drops_a_directive_whose_component_the_candidate_lacks() -> None:
    logs: list[str] = []
    selector = diagnosis.WeightedSelector(lambda candidate: None, logs.append, random.Random(0),
                                          directives=queue_of(entry('a', READ_MD), logs=logs))
    candidate = {k: v for k, v in seed_candidate().items() if k != READ_MD}

    pick = selector(None, None, None, 0, candidate)

    assert len(pick) == 1 and pick[0] in candidate
    assert any('directive a dropped' in line for line in logs)


def test_the_selector_bounds_hold_once_the_directives_are_gone() -> None:
    heavy = [{'cause': 'c', 'excess_chars_per_cell': 1e9, 'evidence': 'e', 'component_hint': READ_MD}]
    selector = diagnosis.WeightedSelector(lambda candidate: heavy, lambda line: None, random.Random(5),
                                          directives=queue_of(entry('a', READ_MD), entry('b', READ_MD)))
    candidate = seed_candidate()

    picks = [selector(None, None, None, 0, candidate)[0] for _ in range(60)]

    assert picks[:2] == [READ_MD, READ_MD]
    assert picks[2] == SRC  # src_patch is overdue, so the first free proposal takes it
    for name in candidate:
        run = longest = 0
        for pick in picks[2:]:
            run = 0 if pick == name else run + 1
            longest = max(longest, run)
        assert longest <= diagnosis.MAX_UNSELECTED, name


# --- dispatcher ---


def dispatch(directive_queue, seen, *, parent=None, names=(READ_MD, SRC), book=None, reflect=None, propose=None):
    def default_reflect(name, text, records):
        seen.append((name, records))
        return text + ' changed'

    def default_propose(candidate, records):
        seen.append((SRC, records))
        return ''

    dispatcher = engine.Dispatcher(engine.StopState(), reflect=reflect or default_reflect,
                                   propose=propose or default_propose, hypotheses=book, directives=directive_queue)
    parent = parent or seed_candidate()
    return dispatcher(parent, {name: [paid_entry()] for name in names}, list(names))


def test_dispatcher_adds_the_directive_record_to_its_own_component_only() -> None:
    queue = queue_of(entry('a', READ_MD))
    queue.take(1)
    seen: list = []

    dispatch(queue, seen)

    records = dict(seen)
    assert {'directive': {'id': 'a', 'brief': 'change a'}} in records[READ_MD]
    assert all('directive' not in record for record in records[SRC])


def test_the_directive_is_consumed_once_per_proposal() -> None:
    queue = queue_of(entry('a', READ_MD))
    queue.take(1)
    seen: list = []

    dispatch(queue, seen)
    dispatch(queue, seen)

    reads = [records for name, records in seen if name == READ_MD]
    assert any('directive' in record for record in reads[0])
    assert not any('directive' in record for record in reads[1])


def test_a_failed_proposal_still_consumes_the_directive() -> None:
    queue = queue_of(entry('a', SRC))
    queue.take(1)

    def boom(candidate, records):
        raise RuntimeError('proposer failed')

    with pytest.raises(RuntimeError):
        dispatch(queue, [], names=(SRC,), propose=boom)

    assert queue.claim() is None


def test_the_directive_hypothesis_fills_in_when_the_reply_has_none(tmp_path) -> None:
    book = diagnosis.HypothesisBook(tmp_path / 'hypotheses.jsonl', lambda line: None)
    queue = queue_of(entry('a', READ_MD, hypothesis=HYPOTHESIS))
    queue.take(1)

    dispatch(queue, [], names=(READ_MD,), book=book)

    stored = (tmp_path / 'hypotheses.jsonl').read_text()
    assert 'calls_per_cell' in stored


def test_the_model_hypothesis_wins_over_the_directive_one(tmp_path) -> None:
    book = diagnosis.HypothesisBook(tmp_path / 'hypotheses.jsonl', lambda line: None)
    queue = queue_of(entry('a', READ_MD, hypothesis=HYPOTHESIS))
    queue.take(1)

    def reflect(name, text, records):
        book.offer(name, f'</new_text> {hypothesis_text("result_chars_per_cell")}')
        return text + ' changed'

    dispatch(queue, [], names=(READ_MD,), book=book, reflect=reflect)

    stored = (tmp_path / 'hypotheses.jsonl').read_text()
    assert 'result_chars_per_cell' in stored
    assert 'calls_per_cell' not in stored


def test_a_directive_does_not_change_the_content_id(tmp_path) -> None:
    parent = seed_candidate()
    plain = dispatch(None, [], parent=parent, names=(READ_MD,))
    queue = queue_of(entry('a', READ_MD))
    queue.take(1)

    seeded = dispatch(queue, [], parent=parent, names=(READ_MD,))

    assert candidates.content_id({**parent, **seeded}) == candidates.content_id({**parent, **plain})


def test_both_headers_tell_the_model_to_implement_a_directive_exactly() -> None:
    for header in (engine.REFLECTION_HEADER, proposer.PROPOSER_HEADER):
        assert diagnosis.DIRECTIVE_NOTE in header
    assert 'implement exactly that change' in diagnosis.DIRECTIVE_NOTE


# --- wiring ---


def test_build_loads_the_shipped_directives_without_a_paid_call(world) -> None:  # noqa: F811
    evo = build(world, '--directives', str(SHIPPED))

    assert not isinstance(evo, int)
    assert [d.id for d in evo.directives.pending][0] == 'search-stable-continuation'
    assert len(evo.directives.pending) == 5


def test_build_refuses_a_bad_directives_file_before_any_work(world, tmp_path, capsys) -> None:  # noqa: F811
    bad = write_file(tmp_path, [entry('a', 'prompts/missing.md')])

    assert build(world, '--directives', str(bad)) == 2
    assert 'prompts/missing.md' in capsys.readouterr().err


def test_without_the_flag_there_is_no_directive_queue_work(world) -> None:  # noqa: F811
    evo = build(world)

    assert not evo.directives.pending
    assert 'directive' not in log_text(world)
