"""The hosted model: Claude through Anthropic's official SDK, behind the same OpenAI-format API
(ADR 0013).

.NET: an adapter, like an IChatClient implementation that wraps another vendor's SDK. The rest of
the gateway only ever sees OpenAI-format requests, replies and SSE chunks, so the rate-limit
settlement (ratelimit.py) works unchanged.

On the way to Claude:
- system and developer messages become the `system` prompt; text parts are joined;
- max_completion_tokens or max_tokens becomes max_tokens (Claude needs one: default from config);
- stop becomes stop_sequences; sampling options Claude rejects (temperature, top_p...) are dropped;
- effort comes from config, and Anthropic's refusal fallback is on: if Claude declines, Anthropic
  retries on another Claude model in the same call.
Text only for now: unsupported_reason() names what the translation can't carry (tools, images,
several choices, JSON mode), and such requests are never sent here.

Gotcha: the SDK uses httpx2, not the httpx the rest of the gateway uses. A connection that breaks
mid-stream raises httpx2's own error, not an SDK error, so the stream catches both.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2
from fastapi.responses import JSONResponse

from tollgate_gateway.errors import openai_error, sse_error_event
from tollgate_gateway.tenants import Tenant

log = logging.getLogger("tollgate.gateway.hosted")

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # Anthropic's beta flag for fallbacks="default"
SYSTEM_ROLES = ("system", "developer")
# OpenAI request fields the translation can't carry yet. Present and non-empty: not supported.
UNSUPPORTED_FIELDS = (
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "logprobs",
    "top_logprobs",
    "audio",
    "modalities",
    "prediction",
)


class DataPolicyError(Exception):
    """A local_only tenant's request reached the hosted model. The router should make this
    impossible; this is the second lock on the same door."""


def _text(content: object) -> str | None:
    """A message's content as plain text, or None if it holds anything else (images, audio...)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts = []
        for part in content:
            if not (isinstance(part, dict) and part.get("type") == "text"):
                return None
            if not isinstance(part.get("text"), str):
                return None
            parts.append(part["text"])
        return "".join(parts)
    return None


def unsupported_reason(body: dict) -> str | None:
    """Why the hosted model can't take this request, or None if it can."""
    for field in UNSUPPORTED_FIELDS:
        if body.get(field):
            return f"The hosted model does not support `{field}` yet."
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type", "text") != "text":
        return "The hosted model does not support `response_format` yet."
    if body.get("n") not in (None, 1):
        return "The hosted model returns one choice: `n` must be 1."
    turns = []
    for message in body.get("messages") or []:
        role = message.get("role") if isinstance(message, dict) else None
        if role in SYSTEM_ROLES:
            if _text(message.get("content")) is None:
                return "The hosted model takes system messages as text only."
            continue
        if role not in ("user", "assistant"):
            return f"The hosted model does not support `{role}` messages yet."
        if message.get("tool_calls") or message.get("function_call"):
            return "The hosted model does not support tool calls yet."
        if not _text(message.get("content")):
            return "The hosted model takes non-empty text messages only."
        turns.append(role)
    if not turns or turns[0] != "user" or turns[-1] != "user":
        return "For the hosted model, the conversation must start and end with a user message."
    return None


def finish_reason(stop_reason: str | None) -> str:
    """Claude's stop_reason as OpenAI's finish_reason."""
    return {"max_tokens": "length", "refusal": "content_filter"}.get(stop_reason or "", "stop")


def _prompt_tokens(usage: Any) -> int:
    return (
        (usage.input_tokens or 0)
        + (getattr(usage, "cache_creation_input_tokens", None) or 0)
        + (getattr(usage, "cache_read_input_tokens", None) or 0)
    )


def _openai_usage(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _attempts_usage(iterations: list) -> dict:
    """After a refusal fallback, every attempt is billed: Anthropic lists each one in
    usage.iterations, while the top-level usage covers only the attempt that answered."""
    return _openai_usage(
        sum(_prompt_tokens(i) for i in iterations), sum(i.output_tokens or 0 for i in iterations)
    )


def to_completion(message: Any, request_id: str) -> dict:
    """A Claude message as an OpenAI chat.completion. Thinking blocks are left out."""
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = message.usage
    if getattr(usage, "iterations", None):
        counts = _attempts_usage(usage.iterations)
    else:
        counts = _openai_usage(_prompt_tokens(usage), usage.output_tokens or 0)
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": message.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason(message.stop_reason),
            }
        ],
        "usage": counts,
    }


class HostedStream:
    """Claude's stream events, translated into OpenAI SSE chunks as they arrive.

    Always ends with a usage chunk (choices: []) before [DONE], like a model server asked for
    include_usage; the gateway strips it again for clients that didn't ask (ratelimit.StreamMeter).
    """

    def __init__(self, events: anthropic.AsyncStream, request_id: str) -> None:
        self.events = events
        self.id = f"chatcmpl-{request_id}"
        self.created = int(time.time())
        self.model = ""
        self.prompt_tokens = 0
        self.stop_reason: str | None = None
        self.final_usage: Any = None

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for event in self.events:
                for chunk in self._translate(event):
                    yield chunk
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            # The 200 status is already sent, so the error goes into the stream instead.
            log.warning("%s: hosted stream broke: %r", self.id, exc)
            yield sse_error_event("The hosted model stopped responding mid-stream.")

    async def close(self) -> None:
        """Close the connection to Anthropic, which stops generation."""
        await self.events.close()

    def _translate(self, event: Any) -> list[bytes]:
        if event.type == "message_start":
            self.model = event.message.model
            self.prompt_tokens = _prompt_tokens(event.message.usage)
            return [self._chunk({"role": "assistant", "content": ""})]
        if event.type == "content_block_delta" and event.delta.type == "text_delta":
            return [self._chunk({"content": event.delta.text})] if event.delta.text else []
        if event.type == "message_delta":
            self.stop_reason = event.delta.stop_reason
            self.final_usage = event.usage
            return []
        if event.type == "message_stop":
            return [
                self._chunk({}, finish_reason(self.stop_reason)),
                self._data({**self._base(), "choices": [], "usage": self._usage()}),
                b"data: [DONE]\n\n",
            ]
        return []  # thinking blocks, signatures, fallback markers, block boundaries

    def _usage(self) -> dict:
        usage = self.final_usage
        if usage is None:
            return _openai_usage(self.prompt_tokens, 0)
        if getattr(usage, "iterations", None):
            return _attempts_usage(usage.iterations)
        # message_delta carries the final output count, and the final input count when it has one.
        prompt = _prompt_tokens(usage) if usage.input_tokens is not None else self.prompt_tokens
        return _openai_usage(prompt, usage.output_tokens or 0)

    def _base(self) -> dict:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
        }

    def _chunk(self, delta: dict, finish: str | None = None) -> bytes:
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        return self._data({**self._base(), "choices": [choice]})

    @staticmethod
    def _data(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode()


class HostedModel:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        effort: str | None,
        default_max_tokens: int,
        refusal_fallback: bool,
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.default_max_tokens = default_max_tokens
        self.refusal_fallback = refusal_fallback

    def params(self, body: dict) -> dict[str, Any]:
        """The Messages API request for an OpenAI chat request. Call unsupported_reason() first."""
        messages = body["messages"]
        system = [_text(m["content"]) for m in messages if m["role"] in SYSTEM_ROLES]
        turns = [
            {"role": m["role"], "content": _text(m["content"])}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.default_max_tokens,  # Claude always needs one
            "messages": turns,
        }
        if system:
            params["system"] = "\n\n".join(s for s in system if s)
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        stop = body.get("stop")
        if stop:
            params["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
        if self.refusal_fallback:
            params["fallbacks"] = "default"
            params["betas"] = [FALLBACK_BETA]
        return params

    def _check(self, tenant: Tenant) -> None:
        if not tenant.allows_hosted:
            raise DataPolicyError(f"tenant {tenant.id} is {tenant.data_policy}")

    async def complete(self, tenant: Tenant, body: dict, request_id: str) -> dict:
        """One whole reply, as an OpenAI chat.completion. Raises the SDK's errors."""
        self._check(tenant)
        message = await self.client.beta.messages.create(**self.params(body))
        return to_completion(message, request_id)

    async def stream(self, tenant: Tenant, body: dict, request_id: str) -> HostedStream:
        """Open a stream. Errors before the first byte raise here, while a proper error status can
        still be sent; errors after that end the stream with an error event."""
        self._check(tenant)
        events = await self.client.beta.messages.create(**self.params(body), stream=True)
        return HostedStream(events, request_id)


def _anthropic_message(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) and message else "The hosted model rejected it."


def hosted_failure(exc: anthropic.APIError) -> JSONResponse:
    """The response for a hosted request that failed before its first byte."""
    if isinstance(exc, anthropic.APITimeoutError):
        return openai_error(
            504, "The hosted model did not respond in time.", "upstream_error", "hosted_timeout"
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return openai_error(
            503, "The hosted model is unavailable.", "upstream_error", "hosted_unavailable"
        )
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in (429, 503, 529):
            # Our organisation's limit or Anthropic's capacity: not the tenant's fault, so not 429.
            return openai_error(
                503, "The hosted model is busy. Try again shortly.", "upstream_error",
                "hosted_unavailable",
            )  # fmt: skip
        if exc.status_code in (400, 413):
            # The request itself, e.g. a prompt longer than the model's context.
            return openai_error(
                400, _anthropic_message(exc), "invalid_request_error", "hosted_rejected"
            )
    # Anything else (our API key, Anthropic's own errors) is ours to fix, not the client's.
    log.error("hosted model failed: %r", exc)
    return openai_error(502, "The hosted model failed.", "upstream_error", "hosted_failed")


def make_hosted(
    *,
    api_key: str | None,
    model: str,
    effort: str | None,
    default_max_tokens: int,
    refusal_fallback: bool,
    timeout_s: float,
    max_retries: int,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> HostedModel | None:
    """The hosted model, or None when no API key is configured (then it is simply off)."""
    if not api_key:
        return None
    http_client = anthropic.DefaultAsyncHttpxClient(transport=transport) if transport else None
    client = anthropic.AsyncAnthropic(
        api_key=api_key,
        # The SDK retries connection errors, 429 and 5xx before the first byte, with backoff.
        max_retries=max_retries,
        timeout=anthropic.Timeout(timeout_s, connect=5.0),
        http_client=http_client,
    )
    return HostedModel(
        client,
        model=model,
        effort=effort,
        default_max_tokens=default_max_tokens,
        refusal_fallback=refusal_fallback,
    )
