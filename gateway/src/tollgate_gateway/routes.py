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
from prometheus_client import CONTENT_TYPE_LATEST
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
from tollgate_gateway.limits import estimate_tokens, prompt_estimate
from tollgate_gateway.ratelimit import (
    Reservation,
    StreamMeter,
    limit_headers,
    rate_limited,
    reserve,
)
from tollgate_gateway.router import Route, choose_route, local_order
from tollgate_gateway.upstream import open_local
from tollgate_gateway.usage import RequestUsage, Status, Tokens, tokens_from_body

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


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """For Prometheus (ADR 0019). Open, like the health checks: timings, no tenant data."""
    return Response(request.app.state.metrics.render(), media_type=CONTENT_TYPE_LATEST)


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
    started_at = request.app.state.clock()
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

    settings = request.app.state.settings
    tenant = principal.tenant
    metrics = request.app.state.metrics
    loads: LoadTracker = request.app.state.loads
    hosted: HostedModel | None = request.app.state.hosted
    admission: Admission | None = request.app.state.admission

    def route_now() -> Route:
        return choose_route(
            loads.snapshot(),
            wants_hosted=chat.model == settings.hosted_model,
            allows_hosted=tenant.allows_hosted,
            hosted_configured=hosted is not None,
            hosted_unsupported=unsupported_reason(payload) if tenant.allows_hosted else None,
        )

    # 1. Route (ADR 0013). In memory, with no Redis call, so it goes first: it decides which Redis
    # calls follow, and a request it turns away needs none.
    route = route_now()
    if route.kind == "refused":
        return route_refused(route)

    # 2. Local, with the fair queue (ADR 0014, 0021): reserve the estimate, join the queue and take
    # a free slot, all in one Redis call. Without a free slot, the request waits its turn.
    if route.kind == "local" and admission is not None:
        return await admit_local(request, admission, route, payload, chat, started_at)

    # 3. Hosted, or local without the queue: reserve the estimate (ADR 0012).
    with metrics.timed("rate_limit"):
        reservation = await reserve(
            request.app.state.buckets, tenant.id, settings.limit_for(tenant.tier), payload
        )
    if reservation is None and request.app.state.buckets is not None:
        metrics.fail_open.labels(step="rate_limit").inc()  # Redis failed: no limit this time
    if reservation is not None and not reservation.decision.allowed:
        return rate_limited(reservation)
    headers = limit_headers(reservation.decision) if reservation else {}
    usage = request_usage(request, reservation, chat, started_at)

    if route.kind == "local":
        # Local without the queue: route again. Choosing a backend and counting the request
        # against it (open_local) must happen with no await in between, so concurrent requests
        # never decide on the same snapshot, and the reservation just awaited Redis.
        route = route_now()
        if route.kind == "refused":  # every backend went down while it reserved: rare
            await usage.refund()
            response = route_refused(route)
            response.headers.update(headers)
            return response
    if route.kind == "hosted":
        assert hosted is not None  # the router only says "hosted" when it is configured
        log.info("request %s: %s -> hosted (%s)", request_id, tenant.id, route.reason)
        return await forward_hosted(hosted, principal, payload, chat, request_id, usage, headers)
    try:
        return await forward_local(request, route, payload, chat, usage, headers)
    except BaseException:
        # A safety net: forward_local records usage on every path it knows.
        with anyio.CancelScope(shield=True):
            await usage.refund()
        raise


def route_refused(route: Route) -> Response:
    type_ = "upstream_error" if route.status >= 500 else "invalid_request_error"
    return openai_error(route.status, route.message, type_, route.code)


def request_usage(
    request: Request,
    reservation: Reservation | None,
    chat: "ChatCompletionRequest",
    started_at: float,
) -> RequestUsage:
    """From here the request ends in exactly one of: usage.record() (a model ran: settle with the
    real count, emit the usage event, ADR 0015) or usage.refund() (no model ran: no event)."""
    principal: Principal = request.state.principal
    return RequestUsage(
        reservation=reservation,
        emitter=request.app.state.usage,
        request_id=request.state.request_id,
        tenant_id=principal.tenant.id,
        credential=credential(principal),
        stream=chat.stream,
        started_at=started_at,
        clock=request.app.state.clock,
    )


async def admit_local(
    request: Request,
    admission: Admission,
    route: Route,
    payload: dict,
    chat: "ChatCompletionRequest",
    started_at: float,
) -> Response:
    """A local request with the fair queue on (ADR 0014, 0021). One Redis call reserves the
    estimate, joins the tenant's queue and takes a free slot if it's this request's turn. If
    there's no free slot, it waits its turn, polling, as before."""
    request_id: str = request.state.request_id
    tenant = request.state.principal.tenant
    settings = request.app.state.settings
    metrics = request.app.state.metrics
    loads: LoadTracker = request.app.state.loads
    limit = settings.limit_for(tenant.tier)
    estimate = estimate_tokens(payload)

    with metrics.timed("admit"):
        first = await admission.first_trip(
            tenant.id, request_id, limit, estimate, loads.available()
        )
    if first is None:  # Redis failed: no limit and no queue (fail open, counted by Admission)
        reservation, headers = None, {}
    else:
        reservation = Reservation(
            request.app.state.buckets, tenant.id, first.decision, estimate, prompt_estimate(payload)
        )
        if first.outcome == "limited":
            return rate_limited(reservation)
        headers = limit_headers(first.decision)
        if first.outcome == "queue_full":
            assert first.joined is not None
            # Its estimate already went back into the bucket, in the same call: nothing to refund.
            response = queue_refused(admission.full_queue(first.joined, loads.available()))
            response.headers.update(headers)
            return response
    usage = request_usage(request, reservation, chat, started_at)

    if first is None:
        ticket = admission.open_ticket(tenant.id, request_id)
    elif first.outcome == "start":
        ticket = admission.ticket(tenant.id, request_id, first.backend)
    else:  # "wait": in the queue, with its reservation
        with metrics.timed("queue"):
            outcome = await admission.wait_turn(
                tenant.id, request_id, loads.available, request.is_disconnected
            )
        if not isinstance(outcome, Ticket):
            await usage.refund()
            if outcome is None:
                return Response(status_code=499)  # the client hung up; nobody reads this
            response = queue_refused(outcome)
            response.headers.update(headers)
            return response
        ticket = outcome

    # The queue says which backend the slot is on; the others stay in the order as failover.
    order = local_order(loads.snapshot()) or route.backends
    if ticket.backend:
        order = (ticket.backend, *(name for name in order if name != ticket.backend))
    route = Route("local", route.reason, order)
    try:
        return await forward_local(request, route, payload, chat, usage, headers, ticket)
    except BaseException:
        # A safety net: forward_local releases the slot and records usage on every path it knows.
        with anyio.CancelScope(shield=True):
            await ticket.release()
            await usage.refund()
        raise


def credential(principal: Principal) -> str:
    """Which credential made the call, for the usage event: an API key's id or a token's subject."""
    if principal.key_id:
        return f"key:{principal.key_id}"
    return f"token:{principal.subject}"


async def forward_local(
    request: Request,
    route: Route,
    payload: dict,
    chat: ChatCompletionRequest,
    usage: RequestUsage,
    headers: dict[str, str],
    ticket: Ticket | None = None,
) -> Response:
    request_id: str = request.state.request_id
    model = request.app.state.settings.local_model  # every local backend serves it (ADR 0005)
    estimate_in = prompt_estimate(payload)
    estimate_out = estimate_tokens(payload) - estimate_in

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
        with request.app.state.metrics.timed("upstream_headers"):
            backend, upstream = await open_local(
                request.app.state.upstream.pick(),
                request.app.state.loads,
                route.backends,
                "/v1/chat/completions",
                request_id=request_id,
                json_body=upstream_body,
            )
    except httpx.HTTPError as exc:
        log.warning("request %s: no local backend answered: %r", request_id, exc)
        await release()
        await usage.refund()  # nothing was generated
        return upstream_failure(exc)
    log.info("request %s: %s -> %s (%s)",
             request_id, request.state.principal.tenant.id, backend, route.reason)  # fmt: skip
    headers = {**headers, "x-tollgate-backend": backend}

    # Plain replies and errors are recorded right away, streams when they end (in relay()).
    if upstream.status_code != 200 or not chat.stream:
        try:
            response = await buffered(upstream)
        finally:
            await release()
        if 200 <= response.status_code < 300:
            tokens = tokens_from_body(bytes(response.body), estimate_in, estimate_out)
            await usage.record(model, backend, "completed", tokens)
        else:
            await usage.refund()  # the model refused or failed: nothing to charge
        response.headers.update(headers)
        return response

    meter = StreamMeter(keep_usage)

    async def close() -> None:
        try:
            await upstream.aclose()
        finally:
            await release()  # the slot goes back when the stream ends, however it ends

    async def end(status: Status) -> None:
        await usage.record(model, backend, status, meter.tokens(estimate_in))

    return StreamingResponse(
        relay(upstream.aiter_bytes(), close, request_id, meter, end),
        media_type="text/event-stream",
        headers={**STREAM_HEADERS, **headers},
    )


async def forward_hosted(
    hosted: HostedModel,
    principal: Principal,
    payload: dict,
    chat: ChatCompletionRequest,
    request_id: str,
    usage: RequestUsage,
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
        await usage.refund()
        return openai_error(403, "Your tenant's data policy keeps requests local.",
                            "invalid_request_error", "data_policy")  # fmt: skip
    except anthropic.APIError as exc:
        log.warning("request %s: hosted model failed before responding: %r", request_id, exc)
        await usage.refund()  # nothing was generated
        response = hosted_failure(exc)
        response.headers.update(headers)
        return response

    if not chat.stream:
        counts = reply["usage"]
        tokens = Tokens(counts["prompt_tokens"], counts["completion_tokens"], "reported")
        await usage.record(reply["model"], "hosted", "completed", tokens)
        return JSONResponse(reply, headers=headers)

    meter = StreamMeter(keep_usage=bool((payload.get("stream_options") or {}).get("include_usage")))
    estimate_in = prompt_estimate(payload)

    async def end(status: Status) -> None:
        # The model that answered: named in the stream's first event (a refusal fallback can
        # change it), or the configured one if the stream broke before that.
        model = stream.model or hosted.model
        await usage.record(model, "hosted", status, meter.tokens(estimate_in))

    return StreamingResponse(
        relay(stream.chunks(), stream.close, request_id, meter, end),
        media_type="text/event-stream",
        headers={**STREAM_HEADERS, **headers},
    )


async def relay(
    chunks: AsyncIterator[bytes],
    close: Callable[[], Awaitable[None]],
    request_id: str,
    meter: StreamMeter,
    end: Callable[[Status], Awaitable[None]],
) -> AsyncIterator[bytes]:
    """Pass each event on as soon as it's complete, watching usage on the way. Works the same for
    a local model server's stream and the hosted model's translated one.

    .NET: an IAsyncEnumerable<byte[]> the server writes out and flushes chunk by chunk.
    Gotcha: if the client disconnects, Starlette cancels this generator, and the CancelledError
    surfaces at the `async for` below. The `finally` then closes the stream (`close`), so the model
    stops generating tokens nobody will read, and records what was used (`end`).
    """
    finished = False
    try:
        async for chunk in chunks:
            for event in meter.feed(chunk):
                yield event
        if rest := meter.flush():
            yield rest
        finished = True
    except httpx.HTTPError as exc:
        # The 200 status is already sent, so the error goes into the stream instead.
        # (The hosted stream reports its own errors this way, in hosted.py.)
        log.warning("request %s: upstream stream broke: %r", request_id, exc)
        meter.failed = True
        yield sse_error_event("The model server stopped responding mid-stream.")
    finally:
        # Starlette runs on anyio, where a cancelled task is cancelled again at *every* await.
        # Shield the cleanup, or it'd be interrupted: connection leaked, usage never recorded.
        with anyio.CancelScope(shield=True):
            await close()
            if meter.failed:
                status: Status = "failed"
            elif finished:
                status = "completed"
            else:
                status = "client_disconnected"
            await end(status)


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
