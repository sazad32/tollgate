"""The HTTP client for the local model servers, and the one retry we allow.

.NET: one HttpClient for the whole app, created at startup, like AddSingleton<HttpClient>.
Gotcha: there's no IHttpClientFactory; the app creates and closes this single instance itself
(main.py, lifespan). It has no base URL: each request names its backend's full URL.
"""

import functools
import logging
import math
import ssl

import httpx

from tollgate_gateway.backends import LoadTracker
from tollgate_gateway.config import Settings

log = logging.getLogger("tollgate.gateway.upstream")

# Failures that happen before the model server has seen the request: the connection was refused,
# or it was closed before any response arrived (llama-server's keep-alive race, bench/RESULTS.md).
# Retrying these can't run the request twice. Anything later, such as a timeout while waiting for
# the first token, is not retried: the model may already be generating.
RETRY_SAFE = (httpx.ConnectError, httpx.RemoteProtocolError)


@functools.cache
def _tls() -> ssl.SSLContext:
    """One TLS context for every pool, made once: each client loads the CA bundle otherwise,
    0.3 s apiece. (The model servers speak plain HTTP today; this is for when they don't.)"""
    return httpx.create_ssl_context()


class UpstreamClients:
    """Several HTTP clients, each with its own small connection pool, taken in turn (ADR 0020).

    Why not one: every time a request starts or ends, httpcore's pool walks all its connections,
    and for each idle one it walks them all again. With ~400 connections in one pool that was a
    quarter of the gateway's CPU. Sixteen pools of 25 do the same job with walks 16 times shorter.
    A response is always read through the client that sent its request, so taking clients in turn
    is safe. .NET: like several HttpClient instances, each with its own SocketsHttpHandler.
    """

    def __init__(self, clients: list[httpx.AsyncClient], per_pool_max: int) -> None:
        self.clients = clients
        self.per_pool_max = per_pool_max
        self._next = 0

    def pick(self) -> httpx.AsyncClient:
        client = self.clients[self._next]
        self._next = (self._next + 1) % len(self.clients)
        return client

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


def make_clients(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> UpstreamClients:
    headers = {}
    if settings.upstream_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key.get_secret_value()}"
    per_pool = max(1, math.ceil(settings.max_upstream_connections / settings.upstream_pools))
    clients = [
        httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout_s,
                read=settings.upstream_read_timeout_s,
                write=10.0,
                pool=10.0,
            ),
            limits=httpx.Limits(
                max_connections=per_pool,
                max_keepalive_connections=per_pool,
                keepalive_expiry=settings.upstream_keepalive_expiry_s,
            ),
            verify=_tls(),
            transport=transport,
        )
        for _ in range(settings.upstream_pools)
    ]
    return UpstreamClients(clients, per_pool)


async def open_local(
    client: httpx.AsyncClient,
    loads: LoadTracker,
    order: tuple[str, ...],
    path: str,
    *,
    request_id: str,
    json_body: object,
) -> tuple[str, httpx.Response]:
    """POST to the first backend in `order` and return (its name, the response) as soon as the
    response headers arrive (the body streams later). The caller must close the response.

    Two attempts in all, and a second one only after a RETRY_SAFE failure: it goes to the next
    backend in `order`, or to the same one again if it is the only one.
    """
    attempts = order[:2] if len(order) > 1 else order * 2
    for number, name in enumerate(attempts, start=1):
        url = loads.backend(name).url + path
        request = client.build_request(
            "POST", url, json=json_body, headers={"X-Request-ID": request_id}
        )
        # Count it now, before the first await: concurrent requests then see it straight away.
        # (Counted after the answer, a burst between two polls all went to the same backend.)
        loads.sent(name)
        try:
            response = await client.send(request, stream=True)
        except RETRY_SAFE as exc:
            if isinstance(exc, httpx.ConnectError) and name not in attempts[number:]:
                loads.mark_down(name)  # refused, and not tried again: keep other requests away
            if number == len(attempts):
                raise
            log.warning("request %s: %s failed before responding (%r), trying %s",
                        request_id, name, exc, attempts[number])  # fmt: skip
            continue
        return name, response
    raise AssertionError("unreachable")
