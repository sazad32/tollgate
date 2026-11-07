"""OpenAI-compatible endpoints, forwarded to the model server.

The gateway is a pass-through proxy: it checks only the fields it needs and forwards the request
body exactly as received, so options it doesn't know (max_tokens, ignore_eos, tools) still work.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tollgate_gateway.errors import openai_error, sse_error_event, upstream_failure
from tollgate_gateway.upstream import open_upstream

log = logging.getLogger("tollgate.gateway")
router = APIRouter()


class ChatCompletionRequest(BaseModel):
    """The fields the gateway itself needs. Everything else is allowed and forwarded untouched."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


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


@router.get("/v1/models")
async def list_models(request: Request) -> Response:
    try:
        upstream = await open_upstream(
            request.app.state.upstream, "GET", "/v1/models", request_id=request.state.request_id
        )
    except httpx.HTTPError as exc:
        return upstream_failure(exc)
    return await buffered(upstream)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    request_id: str = request.state.request_id
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

    try:
        upstream = await open_upstream(
            request.app.state.upstream,
            "POST",
            "/v1/chat/completions",
            request_id=request_id,
            json_body=payload,
        )
    except httpx.HTTPError as exc:
        log.warning("request %s: upstream failed before responding: %r", request_id, exc)
        return upstream_failure(exc)

    if upstream.status_code != 200 or not chat.stream:
        return await buffered(upstream)
    return StreamingResponse(
        relay(upstream, request_id),
        media_type="text/event-stream",
        # no-cache and X-Accel-Buffering stop proxies in between from holding chunks back.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def relay(upstream: httpx.Response, request_id: str) -> AsyncIterator[bytes]:
    """Pass each chunk on as soon as it arrives.

    .NET: an IAsyncEnumerable<byte[]> the server writes out and flushes chunk by chunk.
    Gotcha: if the client disconnects, Starlette cancels this generator, and the CancelledError
    surfaces at the `async for` below. The `finally` then closes the upstream request so the model
    server stops generating tokens nobody will read.
    """
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    except httpx.HTTPError as exc:
        # The 200 status is already sent, so the error goes into the stream instead.
        log.warning("request %s: upstream stream broke: %r", request_id, exc)
        yield sse_error_event("The model server stopped responding mid-stream.")
    finally:
        # Starlette runs on anyio, where a cancelled task is cancelled again at *every* await.
        # Shield the cleanup, or the close itself would be interrupted and the connection leaked.
        with anyio.CancelScope(shield=True):
            await upstream.aclose()


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
