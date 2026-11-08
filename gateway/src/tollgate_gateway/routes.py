"""OpenAI-compatible endpoints, forwarded to the model server.

The gateway is a pass-through proxy: it checks only the fields it needs and forwards the request
body exactly as received, so options it doesn't know (max_tokens, ignore_eos, tools) still work.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tollgate_gateway.auth import Principal, require_principal
from tollgate_gateway.errors import openai_error, sse_error_event, upstream_failure
from tollgate_gateway.ratelimit import (
    Reservation,
    StreamMeter,
    limit_headers,
    rate_limited,
    reserve,
    usage_from_body,
)
from tollgate_gateway.upstream import open_upstream

log = logging.getLogger("tollgate.gateway")
router = APIRouter()  # open: health checks only
# Every /v1 route needs credentials (ADR 0010). Like [Authorize] on a controller.
v1 = APIRouter(dependencies=[Depends(require_principal)])


class ChatCompletionRequest(BaseModel):
    """The fields the gateway itself needs. Everything else is allowed and forwarded untouched."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    # Read for the rate-limit estimate (ADR 0012), so they must be sane numbers.
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz() -> dict[str, str]:
    """Readiness: startup finished. Deliberately not tied to the model server's health: if it were,
    a model-server outage would pull every gateway replica out of the Service, and clients would get
    connection errors instead of a clear 503 from us."""
    return {"status": "ready"}


@v1.get("/v1/models")
async def list_models(request: Request) -> Response:
    try:
        upstream = await open_upstream(
            request.app.state.upstream, "GET", "/v1/models", request_id=request.state.request_id
        )
    except httpx.HTTPError as exc:
        return upstream_failure(exc)
    return await buffered(upstream)


@v1.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    request_id: str = request.state.request_id
    principal: Principal = request.state.principal
    try:
        payload = await request.json()
    except ValueError:
        return openai_error(400, "The request body must be JSON.", "invalid_request_error")
    try:
        chat = ChatCompletionRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "body"
        return openai_error(400, f"{where}: {first['msg']}", "invalid_request_error")

    # 1. Reserve the estimate (ADR 0012). None: no limits applied (not configured, or Redis down).
    settings = request.app.state.settings
    reservation = await reserve(
        request.app.state.buckets,
        principal.tenant.id,
        settings.limit_for(principal.tenant.tier),
        payload,
    )
    if reservation is not None and not reservation.decision.allowed:
        return rate_limited(reservation)
    headers = limit_headers(reservation.decision) if reservation else {}

    # 2. For streams, always ask the model server for its usage report, to settle with real counts.
    upstream_body, keep_usage = payload, True
    if chat.stream:
        options = dict(payload.get("stream_options") or {})
        keep_usage = bool(options.get("include_usage"))
        upstream_body = {**payload, "stream_options": {**options, "include_usage": True}}

    try:
        upstream = await open_upstream(
            request.app.state.upstream,
            "POST",
            "/v1/chat/completions",
            request_id=request_id,
            json_body=upstream_body,
        )
    except httpx.HTTPError as exc:
        log.warning("request %s: upstream failed before responding: %r", request_id, exc)
        if reservation:
            await reservation.settle(0)  # nothing was generated: full refund
        return upstream_failure(exc)

    # 3. Settle: plain replies and errors right away, streams when they end (in relay()).
    if upstream.status_code != 200 or not chat.stream:
        response = await buffered(upstream)
        if reservation:
            ok = 200 <= response.status_code < 300
            used = usage_from_body(response.body) if ok else 0
            await reservation.settle(reservation.reserved if used is None else used)
        response.headers.update(headers)
        return response
    return StreamingResponse(
        relay(upstream, request_id, StreamMeter(keep_usage), reservation),
        media_type="text/event-stream",
        # no-cache and X-Accel-Buffering stop proxies in between from holding chunks back.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
    )


async def relay(
    upstream: httpx.Response,
    request_id: str,
    meter: StreamMeter,
    reservation: Reservation | None,
) -> AsyncIterator[bytes]:
    """Pass each event on as soon as it's complete, watching usage on the way.

    .NET: an IAsyncEnumerable<byte[]> the server writes out and flushes chunk by chunk.
    Gotcha: if the client disconnects, Starlette cancels this generator, and the CancelledError
    surfaces at the `async for` below. The `finally` then closes the upstream request so the model
    server stops generating tokens nobody will read, and settles the reservation.
    """
    try:
        async for chunk in upstream.aiter_bytes():
            for event in meter.feed(chunk):
                yield event
        if rest := meter.flush():
            yield rest
    except httpx.HTTPError as exc:
        # The 200 status is already sent, so the error goes into the stream instead.
        log.warning("request %s: upstream stream broke: %r", request_id, exc)
        yield sse_error_event("The model server stopped responding mid-stream.")
    finally:
        # Starlette runs on anyio, where a cancelled task is cancelled again at *every* await.
        # Shield the cleanup, or it'd be interrupted: connection leaked, bucket left unsettled.
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
            if reservation:
                await reservation.settle(meter.actual(reservation.prompt_estimate))


async def buffered(upstream: httpx.Response) -> Response:
    """Read a whole upstream response and pass it on, mapping server errors to 502."""
    try:
        body = await upstream.aread()
    except httpx.HTTPError as exc:
        return upstream_failure(exc)
    finally:
        await upstream.aclose()
    if upstream.status_code >= 500:
        return openai_error(
            502,
            f"The model server failed (HTTP {upstream.status_code}).",
            "upstream_error",
            "upstream_failed",
        )
    # 2xx answers and 4xx client errors (e.g. prompt too long) pass through as the server sent them.
    return Response(
        content=body,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


router.include_router(v1)
