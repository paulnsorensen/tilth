"""The GEPA engine binding: the only module that imports ``gepa``.

``optimize`` runs gepa 0.1.4's ``optimize_anything`` with evolve's evaluator,
one component dispatcher as ``custom_candidate_proposer`` (so gepa's litellm
reflection model is never built or called), and the shared stop state as a
stop callback.
"""

import json
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence

from gepa.optimize_anything import EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything

from .calls import PaidCalls, StopState
from .candidate import SRC_PATCH

__all__ = ["Dispatcher", "ReflectionClient", "StopState", "Stopper", "optimize", "reflection_prompt"]

REFLECTION_HEADER = (
    "You are revising one instruction file that the tilth MCP server sends to AI coding agents. "
    "Below are its current text and reflective records of agents' rollouts on benchmark tasks: each record "
    "holds the task prompt, the rollout's row fields, and its full tool-call trajectory, and may carry an "
    "applicability label and a critique. Rewrite the file so agents use tilth's tools more effectively on "
    "tasks like these. Keep it concise. Reply with the complete new file text between <new_text> and "
    "</new_text>, and nothing else."
)


def reflection_prompt(text: str, records: list[dict]) -> str:
    """The reflection prompt: a fixed header, the component's current text, and the records, nothing else."""
    rendered = json.dumps(records, indent=2, sort_keys=True)
    return f"{REFLECTION_HEADER}\n\n<current>\n{text}\n</current>\n\n<records>\n{rendered}\n</records>\n"


class Stopper:
    """gepa ``StopperProtocol``: stop once the shared stop state is set."""

    def __init__(self, stop: StopState) -> None:
        self.stop = stop

    def __call__(self, gepa_state: object) -> bool:
        return self.stop.is_set


class ReflectionClient:
    """One isolated, tool-less ``claude -p`` per text component, in a fresh empty cwd and config dir."""

    def __init__(self, paid: PaidCalls, model: str) -> None:
        self.paid = paid
        self.model = model

    def argv(self) -> list[str]:
        # No --bare (it refuses OAuth) and no --mcp-config: --strict-mcp-config then loads no server.
        return ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
                "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                "--no-session-persistence", "--disable-slash-commands"]

    def __call__(self, name: str, text: str, records: list[dict]) -> str:
        with tempfile.TemporaryDirectory(prefix="tilth-reflection-cwd-") as cwd:
            outcome = self.paid.call("reflection", self.argv(), reflection_prompt(text, records), cwd)
        if outcome is None:
            return text
        match = re.search(r"<new_text>(.*?)</new_text>", outcome.text, re.S)
        if match is None:
            self.paid.log(f"reflection: {name}: the reply has no <new_text> block; keeping the parent's text")
            return text
        return match[1].removeprefix("\n")


def _failure(entry: Mapping) -> list[dict]:
    return [{"stage": entry["stage"], "tail": entry["tail"]}] if entry.get("tail") else []


class Dispatcher:
    """gepa ``custom_candidate_proposer``: text components to reflection, ``src_patch`` to the proposer.

    While the stop state is set it makes no call, and once the stop state is set it returns no
    components, so gepa evaluates no child and the stop callback ends the search at the next check.
    """

    def __init__(self, stop: StopState, *, reflect: Callable[[str, str, list[dict]], str],
                 propose: Callable[[Mapping[str, str], list[dict]], str]) -> None:
        self.stop = stop
        self.reflect = reflect
        self.propose = propose

    def __call__(self, candidate: dict[str, str], reflective_dataset: Mapping[str, Sequence[Mapping]],
                 components_to_update: list[str]) -> dict[str, str]:
        proposal = {}
        for name in components_to_update:
            # gepa pads a minibatch with repeated examples; each distinct record goes into a prompt once.
            # An apply, just check, or build failure contributes its stage and tail (tilth build output).
            unique = {json.dumps(record, sort_keys=True): record
                      for entry in reflective_dataset.get(name, ())
                      for record in (*_failure(entry), *entry.get("records", ()))}
            records = list(unique.values())
            if self.stop.is_set:
                break
            if name == SRC_PATCH:
                proposal[name] = self.propose(candidate, records)
            else:
                proposal[name] = self.reflect(name, candidate[name], records)
        return {} if self.stop.is_set else proposal


def optimize(seed: dict[str, str], *, evaluator: Callable, dataset: Sequence[str], max_metric_calls: int,
             stop: StopState, proposer: Dispatcher):
    """Run the GEPA search; returns gepa's result, which evolve never uses to pick finalists."""
    config = GEPAConfig(
        engine=EngineConfig(max_metric_calls=max_metric_calls, parallel=False),
        reflection=ReflectionConfig(reflection_lm=None, custom_candidate_proposer=proposer,
                                    module_selector="round_robin"),
        stop_callbacks=[Stopper(stop)],
    )
    return optimize_anything(dict(seed), evaluator=evaluator, dataset=list(dataset), config=config)
