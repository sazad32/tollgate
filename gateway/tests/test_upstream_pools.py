"""Connections to the model servers, split across several small pools (ADR 0020).

httpcore's pool walks every connection it holds, for every idle one, each time a request starts or
ends: work that grows with the square of the pool's size. At ~400 connections that was a quarter of
the gateway's CPU (bench/RESULTS.md, week 15). Several small pools keep each walk short.
"""

import httpx
import pytest
from tollgate_gateway.config import Settings
from tollgate_gateway.upstream import UpstreamClients, make_clients


def settings(**changes) -> Settings:
    return Settings(api_key_pepper="test", **changes)


@pytest.mark.asyncio
async def test_connections_are_split_across_several_small_pools() -> None:
    up = make_clients(settings(upstream_max_connections=100, upstream_pools=4))
    try:
        assert len(up.clients) == 4
        assert up.per_pool_max == 25
        assert [up.pick() for _ in range(5)] == [*up.clients, up.clients[0]]
        assert len({id(c) for c in up.clients}) == 4
    finally:
        await up.aclose()


@pytest.mark.asyncio
async def test_a_pool_never_gets_a_limit_of_zero() -> None:
    up = make_clients(settings(upstream_max_connections=3, upstream_pools=8))
    try:
        assert up.per_pool_max == 1
    finally:
        await up.aclose()


def test_the_limit_defaults_to_twice_the_backends_slots_with_a_floor() -> None:
    many = [{"name": "a", "url": "http://a", "slots": 2},
            {"name": "mock", "url": "http://mock", "slots": 1000}]  # fmt: skip
    few = [{"name": "vllm", "url": "http://v", "slots": 32},
           {"name": "llama-server-0", "url": "http://l0", "slots": 2}]  # fmt: skip

    assert settings(backends=many).max_upstream_connections == 2004
    assert settings(backends=few).max_upstream_connections == 200  # the floor
    assert settings(backends=many, upstream_max_connections=50).max_upstream_connections == 50


@pytest.mark.asyncio
async def test_every_pool_closes() -> None:
    up = make_clients(settings(upstream_pools=3))
    await up.aclose()

    assert all(c.is_closed for c in up.clients)


@pytest.mark.asyncio
async def test_a_test_transport_reaches_every_pool() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200)

    up = make_clients(settings(upstream_pools=2), httpx.MockTransport(handle))
    try:
        for _ in range(2):
            await up.pick().get("http://mock/metrics")
    finally:
        await up.aclose()

    assert seen == ["mock", "mock"]
    assert isinstance(up, UpstreamClients)
