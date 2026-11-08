"""Shared test setup: the gateway in front of a fake model server, a fake tenant database and a fake
Keycloak. No network, no real model, no real database.

.NET: like WebApplicationFactory<Program> with the HttpClient handler and stores swapped for fakes.
"""

import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import fakeredis
import httpx
import httpx2
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from tollgate_gateway.config import Settings
from tollgate_gateway.jwt_auth import JwksCache, JwtValidator
from tollgate_gateway.keys import display_prefix, generate_key, hash_key
from tollgate_gateway.main import create_app
from tollgate_gateway.tenants import ApiKeyRecord, InMemoryAuthStore, Tenant

CHUNKS = [
    {"choices": [{"delta": {"role": "assistant"}}]},
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}}]},
    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
]
SSE_BODY = "".join(f"data: {json.dumps(c)}\n\n" for c in CHUNKS).encode() + b"data: [DONE]\n\n"
COMPLETION = {
    "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
}

PEPPER = b"test-pepper"
ISSUER = "http://keycloak.test/realms/tollgate"
AUDIENCE = "tollgate-gateway"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


# --- fake model server ---------------------------------------------------------------------------


class FakeUpstream:
    """A model server that answers from a script: each entry is a Response or an exception."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response | Exception] = []

    def default(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "smollm3-3b"}]})
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200, content=SSE_BODY, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=COMPLETION)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return self.default(request)

    @property
    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


# --- fake Anthropic API --------------------------------------------------------------------------

CLAUDE = "claude-opus-5-5"


class FakeAnthropic:
    """Anthropic's Messages API: a reply "Hello" for 12 prompt and 7 output tokens, plain or
    streamed, unless the script says otherwise. Speaks httpx2, the anthropic SDK's HTTP library."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.script: list[httpx2.Response | Exception] = []
        self.iterations: list[dict] | None = None  # per-attempt usage, as after a refusal fallback
        self.break_stream = False  # drop the connection after the first text delta

    def usage(self) -> dict:
        usage: dict = {"input_tokens": 12, "output_tokens": 7}
        if self.iterations:
            usage["iterations"] = self.iterations
        return usage

    def message(self) -> dict:
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},  # omitted by default
                {"type": "text", "text": "Hello"},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": self.usage(),
        }

    def events(self) -> list[dict]:
        start = {**self.message(), "content": [], "stop_reason": None}
        start["usage"] = {"input_tokens": 12, "output_tokens": 1}
        delta_usage: dict = {"output_tokens": 7}
        if self.iterations:
            delta_usage["iterations"] = self.iterations
        thinking = {"type": "thinking", "thinking": "", "signature": ""}
        return [
            {"type": "message_start", "message": start},
            {"type": "content_block_start", "index": 0, "content_block": thinking},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "Hel"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "lo"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": delta_usage},
            {"type": "message_stop"},
        ]  # fmt: skip

    def sse(self) -> httpx2.Response:
        frames = [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in self.events()]
        if not self.break_stream:
            return httpx2.Response(
                200, content=b"".join(frames), headers={"content-type": "text/event-stream"}
            )

        class Broken(httpx2.AsyncByteStream):
            async def __aiter__(self):
                for frame in frames[:6]:  # up to and including the first text delta
                    yield frame
                raise httpx2.ReadError("connection reset")

        return httpx2.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        if json.loads(request.content).get("stream"):
            return self.sse()
        return httpx2.Response(200, json=self.message())

    @property
    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def claude() -> FakeAnthropic:
    return FakeAnthropic()


# --- fake tenant database ------------------------------------------------------------------------


@pytest.fixture
def support_key() -> str:
    return generate_key()


@pytest.fixture
def auth_store(support_key: str) -> InMemoryAuthStore:
    """Two tenants (one disabled), three keys: support's, a revoked one, the disabled tenant's."""
    store = InMemoryAuthStore()
    store.tenants = {
        "support": Tenant(id="support", name="Support team"),
        "billing": Tenant(id="billing", name="Billing team"),
        "legacy": Tenant(id="legacy", name="Retired team", disabled=True),
    }

    def add(key: str, tenant_id: str, key_id: str, revoked: bool = False) -> None:
        store.keys[hash_key(key, PEPPER)] = ApiKeyRecord(
            id=key_id,
            tenant_id=tenant_id,
            prefix=display_prefix(key),
            key_hash=hash_key(key, PEPPER),
            revoked_at=datetime(2026, 11, 1, tzinfo=UTC) if revoked else None,
        )

    add(support_key, "support", "key-support")
    add("tg_revokedrevokedrevokedrevokedrevokedrevok", "support", "key-revoked", revoked=True)
    add("tg_legacylegacylegacylegacylegacylegacylega", "legacy", "key-legacy")
    return store


# --- fake Keycloak -------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_key() -> rsa.RSAPrivateKey:
    """A key Keycloak never published: tokens signed with it are forgeries."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKeycloak:
    """Serves a JWKS document and counts how often it was fetched."""

    def __init__(self, signing_key: rsa.RSAPrivateKey) -> None:
        self.published: dict[str, rsa.RSAPrivateKey] = {"k1": signing_key}
        self.fetches = 0

    def jwks(self) -> dict:
        keys = []
        for kid, private in self.published.items():
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        return httpx.Response(200, json=self.jwks())


@pytest.fixture
def keycloak(signing_key: rsa.RSAPrivateKey) -> FakeKeycloak:
    return FakeKeycloak(signing_key)


@pytest_asyncio.fixture
async def validator(keycloak: FakeKeycloak) -> AsyncIterator[JwtValidator]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(keycloak.handle)) as http:
        yield JwtValidator(JwksCache(http, JWKS_URL), issuer=ISSUER, audience=AUDIENCE)


@pytest.fixture
def make_token(signing_key: rsa.RSAPrivateKey) -> Callable[..., str]:
    """make_token(tenant="support", exp_in=300, key=..., kid="k1", **claims) -> signed JWT."""

    def build(
        tenant: str | None = "support",
        exp_in: int = 300,
        key: rsa.RSAPrivateKey | None = None,
        kid: str = "k1",
        **claims: object,
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "service-account-support",
            "iat": now,
            "exp": now + exp_in,
            **claims,
        }
        if tenant is not None:
            payload["tenant"] = tenant
        return jwt.encode(payload, key or signing_key, algorithm="RS256", headers={"kid": kid})

    return build


# --- the gateway ---------------------------------------------------------------------------------


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    """An in-memory Redis that runs the real Lua scripts."""
    return fakeredis.FakeAsyncRedis()


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_gateway(
    upstream: FakeUpstream,
    auth_store: InMemoryAuthStore,
    keycloak: FakeKeycloak,
    redis: fakeredis.FakeAsyncRedis,
    clock: Clock,
):
    def build(**settings: object):
        settings.setdefault("upstream_url", "http://upstream")
        settings.setdefault("api_key_pepper", PEPPER.decode())
        settings.setdefault("oidc_issuer", ISSUER)
        settings.setdefault("oidc_audience", AUDIENCE)
        settings.setdefault("rate_limits", {"standard": 1_000_000})  # out of the way by default
        return create_app(
            Settings(**settings),
            upstream_transport=httpx.MockTransport(upstream.handle),
            auth_store=auth_store,
            jwks_transport=httpx.MockTransport(keycloak.handle),
            redis_client=redis,
            clock=clock,
        )

    return build


@pytest_asyncio.fixture
async def anonymous(make_gateway: Callable) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client talking to the gateway in-process, sending no credentials."""
    app = make_gateway()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as http,
    ):
        yield http


@pytest_asyncio.fixture
async def client(
    anonymous: httpx.AsyncClient, support_key: str
) -> AsyncIterator[httpx.AsyncClient]:
    """The same, authenticated as the support tenant with its API key."""
    anonymous.headers["Authorization"] = f"Bearer {support_key}"
    yield anonymous


@pytest.fixture
def chat() -> Callable[..., dict]:
    """Build a chat-completion request body: chat(stream=True, max_tokens=5, ...)."""

    def build(stream: bool = False, **extra: object) -> dict:
        return {
            "model": "smollm3-3b",
            "messages": [{"role": "user", "content": "Say hello."}],
            "stream": stream,
            **extra,
        }

    return build


@pytest.fixture
def sse_body() -> bytes:
    return SSE_BODY


@pytest.fixture
def completion() -> dict:
    return COMPLETION
