"""Tolerant JSONL decoding shared by stream parsing and the result store."""

import json


def tolerant_jsonl(raw: str) -> list[dict]:
    """Decode a teed JSONL stream, skipping a line a killed runner left torn."""
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
