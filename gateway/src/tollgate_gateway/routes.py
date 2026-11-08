"""OpenAI-compatible endpoints, routed to a local model server or the hosted model.

Local backends get the request body exactly as received, so options the gateway doesn't know
(max_tokens, ignore_eos, tools) still work. The hosted model gets a translation (hosted.py).
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anthropic
import anyio
import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tollgate_gateway.admission import Admission, Ticket, queue_refused
from tollgate_gateway.auth import Principal, require_principal
from tollgate_gateway.backends import LoadTracker
from tollgate_gateway.errors import openai_error, sse_error_event, upstream_failure
from tollgate_gateway.hosted import (
    DataPolicyError,
    HostedModel,
    hosted_failure,
    unsupported_reason,
)
from tollgate_gateway.ratelimit import (
    Reservation,
    StreamMeter,
    limit_headers,
    rate_limited,
    reserve,
    usage_from_body,
    usage_total,
)
from tollgate_gateway.router import Route, choose_route, local_order
from tollgate_gateway.upstream import open_local

log = logging.getLogger("tollgate.gateway")
router = APIRouter()  # open: health checks only
# Every /v1 route needs credentials (ADR 0010). Like [Authorize] on a controller.
v1 = APIRouter(dependencies=[Depends(require_principal)])

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # no proxy buffering


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
    """Readiness: startup finished. Deliberately not tied to the model servers' health: if it were,
    a model-server outage would pull every gateway replica out of the Service, and clients would get
    connection errors instead of a clear 503 from us."""
    return {"status": "ready"}


@v1.get("/v1/models")
async def list_models(request: Request) -> dict:
    """The models this tenant can ask for, from config: no backend has to be up to answer."""
    settings = request.app.state.settings
    principal: Principal = request.state.principal
    models = [settings.local_model]
    if request.app.state.hosted is not None and principal.tenant.allows_hosted:
        models.append(settings.hosted_model)
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "tollgate"} for m in models],
    }


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
    tenant = principal.tenant
    reservation = await reserve(
        request.app.state.buckets, tenant.id, settings.limit_for(tenant.tier), payload
    )
    if reservation is not None and not reservation.decision.allowed:
        return rate_limited(reservation)
    headers = limit_headers(reservation.decision) if reservation else {}

    # 2. Route (ADR 0013). Choosing a backend and counting the request against it (open_local)
    # happen with no await in between, so concurrent requests never decide on the same snapshot.
    loads: LoadTracker = request.app.state.loads
    hosted: HostedModel | None = request.app.state.hosted
    route = choose_route(
        loads.snapshot(),
        wants_hosted=chat.model == settings.hosted_model,
        allows_hosted=tenant.allows_hosted,
        hosted_configured=hosted is not None,
        hosted_unsupported=unsupported_reason(payload) if tenant.allows_hosted else None,
    )
    if route.kind == "refused":
        if reservation:
            await reservation.settle(0)  # nothing was generated: full refund
        type_ = "upstream_error" if route.status >= 500 else "invalid_request_error"
        response = openai_error(route.status, route.message, type_, route.code)
        response.headers.update(headers)
        return response

    # 3. Forward, and settle when the request ends.
    if route.kind == "hosted":
        assert hosted is not None  # the router only says "hosted" when it is configured
        log.info("request %s: %s -> hosted (%s)", request_id, tenant.id, route.reason)
        return await forward_hosted(
            hosted, principal, payload, chat, request_id, reservation, headers
        )

    # 4. Local: wait for a fair turn at a slot (ADR 0014). The queue says which backend the slot
    # is on; the others stay in the order as failover.
    ticket: Ticket | None = None
    admission: Admission | None = request.app.state.admission
    if admission is not None:
        outcome = await admission.wait_for_slot(
            tenant.id, request_id, loads.available, request.is_disconnected
        )
        if not isinstance(outcome, Ticket):
            if reservation:
                await reservation.settle(0)  # nothing was generated: full refund
            if outcome is None:
                return Response(status_code=499)  # the client hung up; nobody reads this
            response = queue_refused(outcome)
            response.headers.update(headers)
            return response
        ticket = outcome
        order = local_order(loads.snapshot()) or route.backends
        if ticket.backend:
            order = (ticket.backend, *(name for name in order if name != ticket.backend))
        route = Route("local", route.reason, order)
    try:
        return await forward_local(request, route, payload, chat, reservation, headers, ticket)
    except BaseException:
        # A safety net: forward_local releases the slot on every path it knows about.
        if ticket is not None:
            with anyio.CancelScope(shield=True):
                await ticket.release()
        raise


async def forward_local(
    request: Request,
    route: Route,
    payload: dict,
    chat: ChatCompletionRequest,
    reservation: Reservation | None,
    headers: dict[str, str],
    ticket: Ticket | None = None,
) -> Response:
    request_id: str = request.state.request_id

    async def release() -> None:
        if ticket is not None:
            await ticket.release()

    # For streams, always ask the model server for its usage report, to settle with real counts.
    upstream_body, keep_usage = payload, True
    if chat.stream:
        options = dict(payload.get("stream_options") or {})
        keep_usage = bool(options.get("include_usage"))
        upstream_body = {**payload, "stream_options": {**options, "include_usage": True}}

    try:
        backend, upstream = await open_local(
            request.app.state.upstream,
            request.app.state.loads,
            route.backends,
            "/v1/chat/completions",
            request_id=request_id,
            json_body=upstream_body,
        )
    except httpx.HTTPError as exc:
        log.warning("request %s: no local backend answered: %r", request_id, exc)
        await release()
        if reservation:
            await reservation.settle(0)  # nothing was generated: full refund
        return upstream_failure(exc)
    log.info("request %s: %s -> %s (%s)",
             request_id, request.state.principal.tenant.id, backend, route.reason)  # fmt: skip
    headers = {**headers, "x-tollgate-backend": backend}

    # Plain replies and errors settle right away, streams when they end (in relay()).
    if upstream.status_code != 200 or not chat.stream:
        try:
            response = await buffered(upstream)
        finally:
            await release()
        if reservation:
            ok = 200 <= response.status_code < 300
            used = usage_from_body(response.body) if ok else 0
            await reservation.settle(reservation.reserved if used is None else used)
        response.headers.update(headers)
        return response

    async def close() -> None:
        try:
            await upstream.aclose()
        finally:
            await release()  # the slot goes back when the stream ends, however it ends

    return StreamingResponse(
        relay(
            upstream.aiter_bytes(),
            close,
            request_id,
            StreamMeter(keep_usage),
            reservation,
        ),  # fmt: skip
        media_type="text/event-stream",
        headers={**STREAM_HEADERS, **headers},
    )


async def forward_hosted(
    hosted: HostedModel,
    principal: Principal,
    payload: dict,
    chat: ChatCompletionRequest,
    request_id: str,
    reservation: Reservation | None,
    headers: dict[str, str],
) -> Response:
    headers = {**headers, "x-tollgate-backend": "hosted"}
    try:
        if chat.stream:
            stream = await hosted.stream(principal.tenant, payload, request_id)
        else:
            reply = await hosted.complete(principal.tenant, payload, request_id)
    except DataPolicyError:
        log.error("request %s: the router sent a %s request to the hosted model",
                  request_id, principal.tenant.data_policy)  # fmt: skip
        if reservation:
            await reservation.settle(0)
        return openai_error(403, "Your tenant's data policy keeps requests local.",
                            "invalid_request_error", "data_policy")  # fmt: skip
    except anthropic.APIError as exc:
        log.warning("request %s: hosted model failed before responding: %r", request_id, exc)
        if reservation:
            await reservation.settle(0)  # nothing was generated: full refund
        response = hosted_failure(exc)
        response.headers.update(headers)
        return response

    if not chat.stream:
        if reservation:
            used = usage_total(reply["usage"])
            await reservation.settle(reservation.reserved if used is None else used)
        return JSONResponse(reply, headers=headers)
    keep_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
    return StreamingResponse(
        relay(stream.chunks(), stream.close, request_id, StreamMeter(keep_usage), reservation),
        media_type="text/event-stream",
        headers={**STREAM_HEADERS, **headers},
    )


async def relay(
    chunks: AsyncIterator[bytes],
    close: Callable[[], Awaitable[None]],
    request_id: str,
    meter: StreamMeter,
    reservation: Reservation | None,
) -> AsyncIterator[bytes]:
    """Pass each event on as soon as it's complete, watching usage on the way. Works the same for
    a local model server's stream and the hosted model's translated one.

    .NET: an IAsyncEnumerable<byte[]> the server writes out and flushes chunk by chunk.
    Gotcha: if the client disconnects, Starlette cancels this generator, and the CancelledError
    surfaces at the `async for` below. The `finally` then closes the stream (`close`), so the model
    stops generating tokens nobody will read, and settles the reservation.
    """
    try:
        async for chunk in chunks:
            for event in meter.feed(chunk):
                yield event
        if rest := meter.flush():
            yield rest
    except httpx.HTTPError as exc:
        # The 200 status is already sent, so the error goes into the stream instead.
        # (The hosted stream reports its own errors this way, in hosted.py.)
        log.warning("request %s: upstream stream broke: %r", request_id, exc)
        yield sse_error_event("The model server stopped responding mid-stream.")
    finally:
        # Starlette runs on anyio, where a cancelled task is cancelled again at *every* await.
        # Shield the cleanup, or it'd be interrupted: connection leaked, bucket left unsettled.
        with anyio.CancelScope(shield=True):
            await close()
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
