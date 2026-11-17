"""The stream meter must give exactly the same results however it's optimized (ADR 0020).

ReferenceMeter below is StreamMeter as it was before the week 15 optimization, frozen. Thousands
of generated streams go through both, split at random places, and everything that matters for
billing has to match: the events passed on, the usage report, the content chunks counted, and
whether the stream failed.
"""

import json
import random

import pytest
from tollgate_gateway.ratelimit import StreamMeter, usage_total
from tollgate_gateway.usage import Tokens


class ReferenceMeter:
    """StreamMeter before week 15, unchanged. Don't edit it: the new one is checked against it."""

    def __init__(self, keep_usage: bool):
        self.keep_usage = keep_usage
        self.usage: Tokens | None = None
        self.content_chunks = 0
        self.failed = False
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data.replace(b"\r\n", b"\n")
        events = []
        while (end := self._buffer.find(b"\n\n")) >= 0:
            event, self._buffer = self._buffer[: end + 2], self._buffer[end + 2 :]
            if self._inspect(event):
                events.append(event)
        return events

    def flush(self) -> bytes:
        rest, self._buffer = self._buffer, b""
        return rest

    def _inspect(self, event: bytes) -> bool:
        for line in event.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                self.failed = True
            choices = chunk.get("choices") or []
            if any((c.get("delta") or {}).get("content") for c in choices if isinstance(c, dict)):
                self.content_chunks += 1
            usage = chunk.get("usage")
            if usage_total(usage) is not None:
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
                if isinstance(prompt, int) and isinstance(completion, int):
                    self.usage = Tokens(prompt, completion, "reported")
                if not choices and not self.keep_usage:
                    return False
        return True


# --- generated streams ----------------------------------------------------------------------------

TEXTS = ["tok ", "Hello", "", " world", 'say "hi"', "back\\slash", "line\nbreak", "é ü 漢字",
         '"usage"', "usage", '{"error": 1}', "error", '"content":"x"', "data: [DONE]",
         "\n\n"]  # fmt: skip


def data(payload: object) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=random.random() < 0.5).encode() + b"\n\n"


def token(rng: random.Random) -> bytes:
    delta: dict = {"content": rng.choice(TEXTS)}
    if rng.random() < 0.1:
        delta = {"role": "assistant", "content": None}
    chunk = {"id": "c1", "object": "chat.completion.chunk", "model": "m",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}  # fmt: skip
    if rng.random() < 0.1:
        chunk["usage"] = None  # vLLM sends this with every chunk when usage is asked for
    return data(chunk)


def odd_event(rng: random.Random) -> bytes:
    usage = {"prompt_tokens": rng.randint(0, 50), "completion_tokens": rng.randint(0, 300)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return rng.choice([
        data({"choices": [], "usage": usage}),  # the usage-only event
        data({"choices": [{"delta": {}, "finish_reason": "length"}], "usage": usage}),
        data({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        data({"choices": [], "usage": {"total_tokens": 7}}),  # a total, no split
        data({"choices": [], "usage": {"prompt_tokens": 1}}),  # no total at all
        data({"error": {"message": "boom", "type": "server_error"}}),
        data({"error": None, "choices": []}),
        data({"choices": [{"delta": {"content": "a"}}, {"delta": {"content": ""}}]}),
        data({"choices": ["not a dict", {"delta": None}]}),
        data([1, 2]),
        data(5),
        b"data: [DONE]\n\n",
        b"data:\n\n",
        b"data: {not json\n\n",
        b": keep-alive comment\n\n",
        b"event: message\n" + data({"choices": [{"delta": {"content": "x"}}]}),
        b'data: {"choices": [{"delta": {"content": "two"}}]}\n'
        b'data: {"choices": [{"delta": {"content": "lines"}}]}\n\n',
        b"\n\n",
        b"data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]}).encode() + b"   \n\n",
    ])  # fmt: skip


def stream(rng: random.Random) -> bytes:
    events = [
        token(rng) if rng.random() < 0.8 else odd_event(rng) for _ in range(rng.randint(0, 30))
    ]
    raw = b"".join(events)
    if rng.random() < 0.3:
        raw = raw.replace(b"\n", b"\r\n")
    if rng.random() < 0.2:
        raw += b'data: {"choices": [{"delta": {"content": "cut"'  # broken off mid-event
    return raw


def reads(rng: random.Random, raw: bytes) -> list[bytes]:
    """How the bytes arrive: one event per read (the usual case), or cut anywhere."""
    if rng.random() < 0.5:
        parts = raw.replace(b"\r\n", b"\n").split(b"\n\n")
        sep = b"\r\n\r\n" if b"\r\n" in raw else b"\n\n"
        pieces = [p + sep for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        return [p.replace(b"\n", b"\r\n") if sep == b"\r\n\r\n" else p for p in pieces]
    if len(raw) < 2:
        return [raw] if raw else []
    cuts = sorted(rng.sample(range(1, len(raw)), min(len(raw) - 1, rng.randint(0, 40))))
    return [raw[a:b] for a, b in zip([0, *cuts], [*cuts, len(raw)], strict=True) if raw[a:b]]


@pytest.mark.parametrize("seed", range(20))
def test_the_meter_matches_the_reference_on_generated_streams(seed: int) -> None:
    rng = random.Random(seed)
    random.seed(seed)  # json.dumps' ensure_ascii choice in data()
    for _ in range(200):
        keep_usage = rng.random() < 0.5
        new, old = StreamMeter(keep_usage), ReferenceMeter(keep_usage)
        for piece in reads(rng, stream(rng)):
            assert new.feed(piece) == old.feed(piece)
        assert new.flush() == old.flush()
        assert (new.usage, new.content_chunks, new.failed) == (
            old.usage,
            old.content_chunks,
            old.failed,
        )
