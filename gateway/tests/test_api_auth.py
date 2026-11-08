"""What a caller sees over HTTP: /v1 needs credentials, health checks don't."""

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_no_credentials_is_an_openai_style_401(anonymous: httpx.AsyncClient, upstream, chat):
    response = await anonymous.post("/v1/chat/completions", json=chat())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "missing_credentials"
    assert "x-request-id" in response.headers
    assert upstream.requests == []  # refused before reaching the model


async def test_models_need_credentials_too(anonymous: httpx.AsyncClient) -> None:
    assert (await anonymous.get("/v1/models")).status_code == 401


async def test_health_checks_need_none(anonymous: httpx.AsyncClient) -> None:
    assert (await anonymous.get("/healthz")).status_code == 200
    assert (await anonymous.get("/readyz")).status_code == 200


async def test_api_key_gets_through(client: httpx.AsyncClient, chat) -> None:
    assert (await client.post("/v1/chat/completions", json=chat())).status_code == 200


async def test_keycloak_token_gets_through(anonymous: httpx.AsyncClient, make_token, chat):
    response = await anonymous.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    assert response.status_code == 200


async def test_disabled_tenant_gets_403(anonymous: httpx.AsyncClient, chat) -> None:
    response = await anonymous.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": "Bearer tg_legacylegacylegacylegacylegacylegacylega"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_disabled"


async def test_the_tenants_credential_never_reaches_the_model_server(
    client: httpx.AsyncClient, upstream, chat, support_key
) -> None:
    await client.post("/v1/chat/completions", json=chat())

    assert support_key not in str(upstream.requests[-1].headers)
