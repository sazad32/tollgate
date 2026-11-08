"""The HTTP client for the local model servers, and the one retry we allow.

.NET: one HttpClient for the whole app, created at startup, like AddSingleton<HttpClient>.
Gotcha: there's no IHttpClientFactory; the app creates and closes this single instance itself
(main.py, lifespan). It has no base URL: each request names its backend's full URL.
"""

import logging

import httpx

from tollgate_gateway.backends import LoadTracker
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
