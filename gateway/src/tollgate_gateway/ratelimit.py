"""Rate limits around one request: reserve before forwarding, settle once at the end (ADR 0012).

The bucket itself is in limits.py. This module connects it to a request's life:
- reserve() before the model server is called; a refused reservation becomes a 429;
- Reservation.settle() when the request ends, however it ends: with the server's usage report,
  with an estimate from the chunks actually sent (client left), or a full refund (server failed);
- if Redis is down, limits are skipped (fail open) and the request goes through.
"""

import json
import logging
import math
from dataclasses import dataclass

from fastapi.responses import JSONResponse

from tollgate_gateway.errors import openai_error
from tollgate_gateway.limits import Decision, Limit, TokenBuckets, estimate_tokens, prompt_estimate

log = logging.getLogger("tollgate.gateway.ratelimit")


@dataclass
class Reservation:
    """One request's reservation, settled exactly once."""

    buckets: TokenBuckets
    tenant_id: str
    decision: Decision
    reserved: int
    prompt_estimate: int
    settled: bool = False

    async def settle(self, actual: int) -> None:
        if self.settled:
            return
        self.settled = True
        try:
            await self.buckets.settle(self.tenant_id, self.decision.limit, self.reserved, actual)
        except Exception as exc:  # fail open: a missed settlement only skews one bucket briefly
            log.warning("could not settle %s's reservation: %r", self.tenant_id, exc)


async def reserve(
    buckets: TokenBuckets | None, tenant_id: str, limit: Limit, body: dict
) -> Reservation | None:
    """Reserve the request's estimate. None means "no limits applied" (none configured, or Redis is
    down: fail open)."""
    if buckets is None:
        return None
    estimate = estimate_tokens(body)
    try:
        decision = await buckets.reserve(tenant_id, limit, estimate)
    except Exception as exc:
        log.warning("rate limits skipped for %s, Redis unavailable: %r", tenant_id, exc)
        return None
    return Reservation(buckets, tenant_id, decision, estimate, prompt_estimate(body))


def limit_headers(decision: Decision) -> dict[str, str]:
    """OpenAI's rate-limit headers, so clients can pace themselves."""
    return {
        "x-ratelimit-limit-tokens": str(decision.limit.capacity),
        "x-ratelimit-remaining-tokens": str(max(0, math.floor(decision.remaining))),
        "x-ratelimit-reset-tokens": f"{math.ceil(decision.seconds_to_full)}s",
    }


def rate_limited(reservation: Reservation) -> JSONResponse:
    decision = reservation.decision
    wait = max(1, math.ceil(decision.retry_after_s))
    response = openai_error(
        429,
        f"Rate limit reached for tokens: this request needs about {reservation.reserved} and your "
        f"limit is {decision.limit.capacity} per minute. Try again in {wait} s.",
        "tokens",
        "rate_limit_exceeded",
    )
    response.headers.update(limit_headers(decision))
    response.headers["Retry-After"] = str(wait)
    return response


def usage_total(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    if isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"]
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt + completion
    return None


def usage_from_body(body: bytes) -> int | None:
    """Total tokens from a non-streamed reply, if the server reported them."""
    try:
        return usage_total(json.loads(body).get("usage"))
    except (ValueError, AttributeError):
        return None


class StreamMeter:
    """Watches a streamed reply on its way through: splits it into SSE events, reads the usage
    report, counts content chunks, and drops the usage-only event if the client didn't ask for it
    (the gateway always asks upstream, so it can settle with the real count)."""

    def __init__(self, keep_usage: bool):
        self.keep_usage = keep_usage
        self.usage: int | None = None
        self.content_chunks = 0
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        """Complete events to pass on, in order. A partial event waits for the rest of its bytes."""
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

    def actual(self, prompt_estimate: int) -> int:
        """The count to settle with: the server's, or prompt estimate + one token per chunk
        sent. llama-server streams one token per chunk; vLLM can pack several, so this may
        undercount there."""
        if self.usage is not None:
            return self.usage
        return prompt_estimate + self.content_chunks

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
            choices = chunk.get("choices") or []
            if any((c.get("delta") or {}).get("content") for c in choices if isinstance(c, dict)):
                self.content_chunks += 1
            total = usage_total(chunk.get("usage"))
            if total is not None:
                self.usage = total
                if not choices and not self.keep_usage:
                    return False
        return True
