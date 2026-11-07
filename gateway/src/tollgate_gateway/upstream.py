"""The HTTP client for the model server, and the one retry we allow.

.NET: one HttpClient for the whole app, created at startup, like AddSingleton<HttpClient>.
Gotcha: there's no IHttpClientFactory; the app creates and closes this single instance itself
(main.py, lifespan).
"""

import logging

import httpx

from tollgate_gateway.config import Settings

log = logging.getLogger("tollgate.gateway.upstream")

# Failures that happen before the model server has seen the request: the connection was refused,
# or it was closed before any response arrived (llama-server's keep-alive race, bench/RESULTS.md).
# Retrying these can't run the request twice. Anything later, such as a timeout while waiting for
# the first token, is not retried: the model may already be generating.
RETRY_SAFE = (httpx.ConnectError, httpx.RemoteProtocolError)


def make_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    headers = {}
    if settings.upstream_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key.get_secret_value()}"
    return httpx.AsyncClient(
        base_url=settings.upstream_url,
        headers=headers,
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=settings.upstream_read_timeout_s,
            write=10.0,
            pool=10.0,
        ),
        limits=httpx.Limits(
            max_connections=settings.upstream_max_connections,
            keepalive_expiry=settings.upstream_keepalive_expiry_s,
        ),
        transport=transport,
    )


async def open_upstream(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    request_id: str,
    json_body: object | None = None,
) -> httpx.Response:
    """Send a request and return as soon as the response headers arrive (the body streams later).

    Retries exactly once, and only for RETRY_SAFE failures. The caller must close the response.
    """
    headers = {"X-Request-ID": request_id}
    for attempt in (1, 2):
        request = client.build_request(method, path, json=json_body, headers=headers)
        try:
            return await client.send(request, stream=True)
        except RETRY_SAFE as exc:
            if attempt == 2:
                raise
            log.warning("request %s: upstream failed before responding (%r), retrying once",
                        request_id, exc)  # fmt: skip
    raise AssertionError("unreachable")
