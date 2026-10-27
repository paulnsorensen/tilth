"""The GEPA engine binding: the only module that imports ``gepa``.

``optimize`` runs gepa 0.1.4's ``optimize_anything`` with evolve's evaluator,
one component dispatcher as ``custom_candidate_proposer`` (so gepa's litellm
reflection model is never built or called), and the shared stop state as a
stop callback.
"""

import json
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence

from gepa.optimize_anything import EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything

from .calls import PaidCalls, StopState
from .candidate import MAX_BYTES, OBJECTIVE, SRC_PATCH, content_id
from .diagnosis import DIAGNOSIS_NOTE, DIRECTIVE_NOTE, REFLECTION_HYPOTHESIS, HypothesisBook
from .source_map import entries_for

__all__ = ["Dispatcher", "ReflectionClient", "StopState", "Stopper", "optimize", "reflection_prompt"]

REFLECTION_HEADER = (
    "You are revising one instruction file that the tilth MCP server sends to AI coding agents. "
    "Below are its current text and reflective records of agents' rollouts on benchmark tasks: each record "
    "holds the task prompt, the rollout's row fields, and its full tool-call trajectory, and may carry an "
    f"applicability label and a critique. {OBJECTIVE} Rewrite the file so agents solve tasks like these at "
    "lower cost without losing correctness. Keep it concise. "
    f"{DIAGNOSIS_NOTE} {DIRECTIVE_NOTE} Reply with the complete new file text between <new_text> and </new_text>. "
    f"{REFLECTION_HYPOTHESIS}"
)
# The paid-tier side_info fields that make up the score; reflection sees them once per candidate.
REPAIR_INSTRUCTION = "Fix only this failure; keep the intended change."
SCORE_FIELDS = ("task_scores", "pass_rate", "mean_cost", "baseline_cost")
SIZE_NOTE = ("The current file is {size} bytes. The new file must be at most {max_bytes} UTF-8 bytes, the size of "
             "the MCP field it fills, so remove at least as much text as you add; a longer file is refused "
             "and never scored.")
SHORTEN_PROMPT = ("Your rewrite of one instruction file for the tilth MCP server is {size} UTF-8 bytes. Its MCP "
                  "field holds at most {max_bytes} bytes. Shorten the file to at most {max_bytes} bytes "
                  "and keep its most important guidance. Reply with the complete new file text between <new_text> "
                  "and </new_text>, and nothing else.")


def reflection_prompt(text: str, records: list[dict], *, max_bytes: int | None = None) -> str:
    """The reflection prompt: a fixed header (with the byte cap when the file has one), the text, and the records."""
    note = None if max_bytes is None else SIZE_NOTE.format(size=len(text.encode("utf-8")), max_bytes=max_bytes)
    header = REFLECTION_HEADER if note is None else f"{REFLECTION_HEADER} {note}"
    rendered = json.dumps(records, indent=2, sort_keys=True)
    return f"{header}\n\n<current>\n{text}\n</current>\n\n<records>\n{rendered}\n</records>\n"


class Stopper:
    """gepa ``StopperProtocol``: stop once the shared stop state is set."""

    def __init__(self, stop: StopState) -> None:
        self.stop = stop

    def __call__(self, gepa_state: object) -> bool:
        return self.stop.is_set


class ReflectionClient:
    """One isolated, tool-less ``claude -p`` per text component, in a fresh empty cwd and config dir."""

    def __init__(self, paid: PaidCalls, model: str, hypotheses: HypothesisBook | None = None) -> None:
        self.paid = paid
        self.model = model
        self.hypotheses = hypotheses

    def argv(self) -> list[str]:
        # No --bare (it refuses OAuth) and no --mcp-config: --strict-mcp-config then loads no server.
        return ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
                "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                "--no-session-persistence", "--disable-slash-commands"]

    def __call__(self, name: str, text: str, records: list[dict]) -> str:
        limit = MAX_BYTES.get(name)
        new = self._ask(name, reflection_prompt(text, records, max_bytes=limit), offer=True)
        if new is None:
            return text
        size = len(new.encode("utf-8"))
        if limit is not None and size > limit:
            # The materializer refuses an oversize text, so one follow-up call is cheaper than a lost candidate.
            self.paid.log(f"reflection: {name}: the reply is {size} bytes, over the {limit}-byte cap; "
                          "asking once to shorten it")
            prompt = f"{SHORTEN_PROMPT.format(size=size, max_bytes=limit)}\n\n<current>\n{new}\n</current>\n"
            new = self._ask(name, prompt) or new
        return new

    def _ask(self, name: str, prompt: str, *, offer: bool = False) -> str | None:
        """One isolated call; the ``<new_text>`` block of the reply, or None when there is none.

        With ``offer``, a hypothesis block after ``</new_text>`` goes to the hypothesis book.
        """
        with tempfile.TemporaryDirectory(prefix="tilth-reflection-cwd-") as cwd:
            outcome = self.paid.call("reflection", self.argv(), prompt, cwd)
        if outcome is None:
            return None
        match = re.search(r"<new_text>(.*?)</new_text>", outcome.text, re.S)
        if match is None:
            self.paid.log(f"reflection: {name}: the reply has no <new_text> block; keeping the earlier text")
            return None
        if offer and self.hypotheses is not None:
            self.hypotheses.offer(name, outcome.text[match.end():])
        return match[1].removeprefix("\n")


def _failure(entry: Mapping) -> list[dict]:
    return [{"stage": entry["stage"], "tail": entry["tail"]}] if entry.get("tail") else []


def _diagnosis(entry: Mapping) -> list[dict]:
    """A candidate's attribution view, and the source entries that render its leading causes."""
    view = entry.get("attribution")
    if not view:
        return []
    sources = entries_for(d["cause"] for d in view.get("dominant", ()))
    return [{"attribution": view}, {"source_map": sources}]


def _cost(entry: Mapping) -> list[dict]:
    """A paid entry's score inputs and its task's candidate/baseline cost pairs; nothing for other stages."""
    if entry.get("stage") != "paid":
        return []
    scores = {"scores": {name: entry[name] for name in SCORE_FIELDS if name in entry}}
    return [scores, {"cost_pairs": entry["cost_pairs"]}] if entry.get("cost_pairs") else [scores]


class Dispatcher:
    """gepa ``custom_candidate_proposer``: text components to reflection, ``src_patch`` to the proposer.

    While the stop state is set, it makes no call.
    Once the stop state is set, it returns no components, so gepa evaluates no child.
    The stop callback then ends the search at the next check.
    """

    def __init__(self, stop: StopState, *, reflect: Callable[[str, str, list[dict]], str],
                 propose: Callable[[Mapping[str, str], list[dict]], str],
                 hypotheses: HypothesisBook | None = None,
                 extra_records: Callable[[Mapping[str, str]], list[dict]] | None = None,
                 directives=None,
                 check: Callable[[Mapping[str, str]], tuple[str, str] | None] | None = None,
                 failing_tests: Callable[[Mapping[str, str]], list[str]] | None = None,
                 test_scope: dict[str, frozenset[str]] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.failing_tests = failing_tests
        # Content id -> tests that candidate may change; the materializer reads the same dict.
        self.test_scope = test_scope if test_scope is not None else {}
        self.directives = directives
        self.check = check
        self.log = log or (lambda _line: None)
        self.stop = stop
        self.reflect = reflect
        self.propose = propose
        self.hypotheses = hypotheses
        self.extra_records = extra_records

    def __call__(self, candidate: dict[str, str], reflective_dataset: Mapping[str, Sequence[Mapping]],
                 components_to_update: list[str]) -> dict[str, str]:
        proposal = {}
        patch_records: list[dict] = []
        directive = self.directives.claim() if self.directives is not None else None
        if self.hypotheses is not None:
            self.hypotheses.discard()
        for name in components_to_update:
            # gepa pads a minibatch with repeated examples; each distinct record goes into a prompt once.
            # An apply, just check, or build failure contributes its stage and tail (tilth build output);
            # a paid entry contributes the score inputs and its task's cost pairs.
            unique = {json.dumps(record, sort_keys=True): record
                      for entry in reflective_dataset.get(name, ())
                      for record in (*_failure(entry), *_cost(entry), *_diagnosis(entry), *entry.get("records", ()))}
            if self.extra_records is not None:
                unique.update({json.dumps(record, sort_keys=True): record for record in self.extra_records(candidate)})
            if directive is not None and directive.component == name:
                unique[json.dumps(directive.record(), sort_keys=True)] = directive.record()
            records = list(unique.values())
            if self.stop.is_set:
                break
            if name == SRC_PATCH:
                patch_records = records
                proposal[name] = self.propose(candidate, records)
            else:
                proposal[name] = self.reflect(name, candidate[name], records)
            if directive is not None and directive.component == name and directive.hypothesis \
                    and self.hypotheses is not None:
                self.hypotheses.default(name, directive.hypothesis)
        if SRC_PATCH in proposal and not self.stop.is_set:
            self._inherit_tests(candidate, proposal)
        if SRC_PATCH in proposal and self.check is not None and not self.stop.is_set:
            proposal[SRC_PATCH] = self._repaired(candidate, proposal, patch_records)
        if self.stop.is_set:
            return {}
        if self.hypotheses is not None:
            self.hypotheses.attach(content_id(candidate), content_id({**candidate, **proposal}))
        return proposal

    def _repaired(self, candidate: Mapping[str, str], proposal: Mapping[str, str], records: list[dict]) -> str:
        """The proposed ``src_patch``, or one repair of it when its child fails apply or ``just check``.

        The repair is the proposer's only second call: it sees the failed patch applied and the real error.
        Whatever it returns is the child; the cascade then checks that child (the check is memoized) and fails
        it for good if it still fails. The same directive and hypothesis stay on the final child.

        When ``just check`` failed on Rust tests, the repair record names them and sets ``may_update_tests``.
        The final child may then change exactly those tests, and so may its descendants.
        """
        if proposal[SRC_PATCH] == candidate[SRC_PATCH]:
            return proposal[SRC_PATCH]  # no change: the parent was checked when it was evaluated
        child = {**candidate, **proposal}
        failure = self.check(child)
        if failure is None:
            return proposal[SRC_PATCH]
        stage, error = failure
        repair = {"stage": stage, "error": error, "instruction": REPAIR_INSTRUCTION}
        failing = self.failing_tests(child) if stage == "just check" and self.failing_tests is not None else []
        if failing:
            repair["failing_tests"] = failing
            repair["may_update_tests"] = True
        if stage == "apply":
            # The failed patch cannot be applied to an export, so the proposer gets it as text.
            repair["failed_diff"] = proposal[SRC_PATCH]
            base = candidate
        else:
            base = child
        try:
            patch = self.propose(base, [*records, {"repair": repair}])
        except (OSError, RuntimeError, ValueError) as error:
            # A failed repair leaves the failed child; stop and auth signals are not caught here.
            self.log(f"repair: proposer failed ({type(error).__name__}: {error}); keeping the failed patch")
            return proposal[SRC_PATCH]
        if failing:
            self._grant_tests({**candidate, **proposal, SRC_PATCH: patch}, failing)
        return patch

    def _grant_tests(self, child: Mapping[str, str], names: Iterable[str]) -> None:
        """Let ``child``, and so its materialization, change the named tests; earlier grants stay."""
        cid = content_id(child)
        self.test_scope[cid] = self.test_scope.get(cid, frozenset()) | frozenset(names)

    def _inherit_tests(self, candidate: Mapping[str, str], proposal: Mapping[str, str]) -> None:
        """A child's cumulative patch keeps its parent's test edits, so it keeps the parent's grant too."""
        granted = self.test_scope.get(content_id(candidate))
        if granted:
            self._grant_tests({**candidate, **proposal}, granted)


def optimize(seed: dict[str, str], *, evaluator: Callable, dataset: Sequence[str], max_metric_calls: int,
             stop: StopState, proposer: Dispatcher, selector: Callable | None = None):
    """Run the GEPA search; returns gepa's result, which evolve never uses to pick finalists."""
    config = GEPAConfig(
        engine=EngineConfig(max_metric_calls=max_metric_calls, parallel=False),
        reflection=ReflectionConfig(reflection_lm=None, custom_candidate_proposer=proposer,
                                    module_selector=selector or "round_robin"),
        stop_callbacks=[Stopper(stop)],
    )
    return optimize_anything(dict(seed), evaluator=evaluator, dataset=list(dataset), config=config)
