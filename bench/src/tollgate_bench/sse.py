"""Parse the server-sent events (SSE) of an OpenAI-compatible streamed chat completion.

A stream is lines of text. Data lines look like `data: {...json...}`, the last one is
`data: [DONE]`, and blank lines separate events.
"""

import json


def parse_line(line: str) -> dict | None:
    """Return the JSON payload of a data line, or None for blank, comment and [DONE] lines."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    return json.loads(payload)


def has_content(chunk: dict) -> bool:
    """True if the chunk carries generated text (the first such chunk marks time to first token)."""
    return any((choice.get("delta") or {}).get("content") for choice in chunk.get("choices", []))
