import json

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_stream_is_relayed_unchanged(client: httpx.AsyncClient, chat, sse_body) -> None:
    # A client that asks for usage gets the stream byte for byte. (One that doesn't ask has the
    # usage chunk removed again, since the gateway always requests it: test_rate_limit_api.py.)
    body = chat(stream=True, stream_options={"include_usage": True})
    response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == sse_body  # every chunk, in order, byte for byte, including [DONE]


async def test_non_stream_returns_the_upstream_json(
    client: httpx.AsyncClient, chat, completion
) -> None:
    response = await client.post("/v1/chat/completions", json=chat())

    assert response.status_code == 200
    assert response.json() == completion


async def test_unknown_fields_are_forwarded_untouched(
    client: httpx.AsyncClient, upstream, chat
) -> None:
    body = chat(max_tokens=128, ignore_eos=True, temperature=0)

    await client.post("/v1/chat/completions", json=body)

    assert upstream.last_body == body


async def test_request_id_is_returned_and_sent_upstream(
    client: httpx.AsyncClient, upstream, chat
) -> None:
    response = await client.post("/v1/chat/completions", json=chat())

    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert upstream.requests[-1].headers["x-request-id"] == request_id


async def test_each_request_gets_its_own_id(client: httpx.AsyncClient, chat) -> None:
    first = await client.post("/v1/chat/completions", json=chat())
    second = await client.post("/v1/chat/completions", json=chat())

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_client_supplied_request_id_is_ignored(client: httpx.AsyncClient, chat) -> None:
    response = await client.post(
        "/v1/chat/completions", json=chat(), headers={"X-Request-ID": "chosen-by-client"}
    )

    assert response.headers["x-request-id"] != "chosen-by-client"


async def test_upstream_key_is_sent_and_the_clients_auth_is_not(
    make_gateway, upstream, chat, support_key
) -> None:
    app = make_gateway(upstream_api_key="upstream-secret")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as http,
    ):
        await http.post(
            "/v1/chat/completions", json=chat(), headers={"Authorization": f"Bearer {support_key}"}
        )

    assert upstream.requests[-1].headers["authorization"] == "Bearer upstream-secret"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"not json", "must be JSON"),
        (json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(), "model"),
        (json.dumps({"model": "m", "messages": []}).encode(), "messages"),
        (json.dumps(["a", "list"]).encode(), "body"),
    ],
)
async def test_bad_requests_get_an_openai_style_400(
    client: httpx.AsyncClient, upstream, body: bytes, fragment: str
) -> None:
    response = await client.post(
        "/v1/chat/completions", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert fragment in error["message"]
    assert upstream.requests == []  # never forwarded


async def test_list_models_comes_from_config(client: httpx.AsyncClient, upstream) -> None:
    # The gateway knows its models; it doesn't ask a backend that may be down (ADR 0013).
    response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "smollm3-3b"
    assert upstream.requests == []


async def test_health_endpoints(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}
