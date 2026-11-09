"""A fake model server for load tests (ADR 0019): OpenAI-compatible, no GPU, tokens at a set pace.

It answers /v1/chat/completions like llama-server with `ignore_eos`: exactly `max_tokens` tokens,
the first after `first_token_s`, the rest at `tokens_per_s`, then a usage report. Its /metrics
look like llama-server's, so the gateway's load poller treats it as one. With the GPU out of the
way, a load test measures the gateway itself.

A bare ASGI app, not FastAPI: the less work per token here, the more streams one process can pace,
and the mock must never be the thing that breaks. Run it with several uvicorn workers:
    uvicorn tollgate_bench.mock:create_app --factory --workers 4
Settings come from MOCK_TOKENS_PER_S and MOCK_FIRST_TOKEN_MS.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass

DONE = b"data: [DONE]\n\n"


@dataclass(frozen=True)
class MockSettings:
    tokens_per_s: float = 20.0  # like a real model streaming to one user
    first_token_s: float = 0.1  # reading the prompt
    max_tokens: int = 32  # when the request doesn't say

    @classmethod
    def from_env(cls) -> "MockSettings":
        return cls(
            tokens_per_s=float(os.environ.get("MOCK_TOKENS_PER_S", "20")),
            first_token_s=float(os.environ.get("MOCK_FIRST_TOKEN_MS", "100")) / 1000,
        )


def prompt_tokens(messages: list) -> int:
    """About four characters a token, like the tokenizers do for English."""
    chars = sum(len(m.get("content") or "") for m in messages if isinstance(m, dict))
    return max(1, chars // 4)


def _event(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


class MockServer:
    def __init__(self, settings: MockSettings) -> None:
        self.settings = settings
        self.running = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while (await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        path, method = scope["path"], scope["method"]
        if method == "GET" and path == "/metrics":
            text = f"llamacpp:requests_processing {self.running}\nllamacpp:requests_deferred 0\n"
            await _respond(send, 200, text.encode(), b"text/plain; version=0.0.4")
        elif method == "GET" and path == "/health":
            await _respond(send, 200, b'{"status":"ok"}', b"application/json")
        elif method == "POST" and path == "/v1/chat/completions":
            await self.chat(receive, send)
        else:
            await _respond(send, 404, b'{"error":{"message":"not found"}}', b"application/json")

    async def chat(self, receive, send) -> None:
        raw = b""
        while True:
            message = await receive()
            raw += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            body = json.loads(raw)
            messages = body["messages"]
        except (ValueError, KeyError, TypeError):
            await _respond(send, 400, b'{"error":{"message":"bad request"}}', b"application/json")
            return
        model = body.get("model", "mock-model")
        n_out = int(body.get("max_tokens") or self.settings.max_tokens)
        n_in = prompt_tokens(messages)
        usage = {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}
        self.running += 1
        try:
            if body.get("stream"):
                include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                await self.stream(send, model, n_out, usage if include_usage else None)
            else:
                await asyncio.sleep(
                    self.settings.first_token_s + (n_out - 1) / self.settings.tokens_per_s
                )
                reply = {"id": "mock", "object": "chat.completion", "model": model,
                         "choices": [{"index": 0, "finish_reason": "length",
                                      "message": {"role": "assistant", "content": "tok " * n_out}}],
                         "usage": usage}  # fmt: skip
                await _respond(send, 200, json.dumps(reply).encode(), b"application/json")
        finally:
            self.running -= 1

    async def stream(self, send, model: str, n_out: int, usage: dict | None) -> None:
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})  # fmt: skip
        token = _event({"id": "mock", "object": "chat.completion.chunk", "model": model,
                        "choices": [{"index": 0, "delta": {"content": "tok "}}]})  # fmt: skip
        # A fixed schedule from the start, so a late wake-up doesn't push every later token back.
        start = time.perf_counter() + self.settings.first_token_s
        interval = 1 / self.settings.tokens_per_s
        for i in range(n_out):
            delay = start + i * interval - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            await send({"type": "http.response.body", "body": token, "more_body": True})
        tail = _event(
            {
                "id": "mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            }
        )
        if usage is not None:
            tail += _event({"id": "mock", "object": "chat.completion.chunk", "model": model,
                            "choices": [], "usage": usage})  # fmt: skip
        await send({"type": "http.response.body", "body": tail + DONE, "more_body": False})


async def _respond(send, status: int, body: bytes, content_type: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type)]})  # fmt: skip
    await send({"type": "http.response.body", "body": body})


def create_app(settings: MockSettings | None = None) -> MockServer:
    return MockServer(settings or MockSettings.from_env())
