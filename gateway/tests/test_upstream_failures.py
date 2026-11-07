"""What the gateway does when the model server fails, and the one retry it allows."""

import httpx
import pytest

pytestmark = pytest.mark.asyncio

CLOSED_EARLY = httpx.RemoteProtocolError("Server disconnected without sending a response.")


async def test_retries_once_when_upstream_closes_before_responding(
    client: httpx.AsyncClient, upstream, chat
) -> None:
    upstream.script = [CLOSED_EARLY]  # first attempt hits the keep-alive race, second succeeds

    response = await client.post("/v1/chat/completions", json=chat(stream=True))

    assert response.status_code == 200
    assert len(upstream.requests) == 2


async def test_gives_up_after_one_retry(client: httpx.AsyncClient, upstream, chat) -> None:
    upstream.script = [CLOSED_EARLY, CLOSED_EARLY, CLOSED_EARLY]

    response = await client.post("/v1/chat/completions", json=chat())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"
    assert len(upstream.requests) == 2


async def test_unreachable_upstream_is_503(client: httpx.AsyncClient, upstream, chat) -> None:
    upstream.script = [httpx.ConnectError("refused"), httpx.ConnectError("refused")]

    response = await client.post("/v1/chat/completions", json=chat())

    assert response.status_code == 503


async def test_timeout_is_504_and_never_retried(client: httpx.AsyncClient, upstream, chat) -> None:
    # The model may already be generating; a retry could run the request twice.
    upstream.script = [httpx.ReadTimeout("no first byte")]

    response = await client.post("/v1/chat/completions", json=chat(stream=True))

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"
    assert len(upstream.requests) == 1


async def test_upstream_server_error_becomes_502(client: httpx.AsyncClient, upstream, chat) -> None:
    upstream.script = [httpx.Response(500, text="CUDA error: out of memory")]

    response = await client.post("/v1/chat/completions", json=chat(stream=True))

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"
    assert "CUDA" not in response.text  # internal details stay internal
    assert len(upstream.requests) == 1  # a server error is a response: no retry


async def test_upstream_client_error_passes_through(
    client: httpx.AsyncClient, upstream, chat
) -> None:
    error = {"error": {"message": "prompt is too long", "type": "invalid_request_error"}}
    upstream.script = [httpx.Response(400, json=error)]

    response = await client.post("/v1/chat/completions", json=chat(stream=True))

    assert response.status_code == 400
    assert response.json() == error
