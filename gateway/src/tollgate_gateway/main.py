"""The gateway app. Run it with: uvicorn --factory tollgate_gateway.main:create_app

.NET: create_app() is Program.cs: build the app, register its pieces, return it. Uvicorn (the
Kestrel of Python) calls it and runs the result.
Gotcha: nothing is created at import time. The upstream client is made in `lifespan`, Python's
IHostedService.StartAsync/StopAsync: code before `yield` runs at startup, after it at shutdown.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate_gateway.config import Settings
from tollgate_gateway.routes import router
from tollgate_gateway.upstream import make_client


class RequestIdMiddleware:
    """Give every request an ID: in request.state, sent upstream, returned as X-Request-ID.

    Generated here, never taken from the client: week 10 builds billing idempotency keys on it.
    A plain ASGI middleware rather than @app.middleware("http"), which wraps the response stream
    and can get in the way of streaming and client-disconnect handling.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.upstream = make_client(app.state.settings, app.state.upstream_transport)
    try:
        yield
    finally:
        await app.state.upstream.aclose()


def create_app(
    settings: Settings | None = None, upstream_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the app. Tests pass settings and a fake upstream transport; Uvicorn passes neither."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = FastAPI(title="Tollgate gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or Settings()
    app.state.upstream_transport = upstream_transport
    app.include_router(router)
    app.add_middleware(RequestIdMiddleware)
    return app
