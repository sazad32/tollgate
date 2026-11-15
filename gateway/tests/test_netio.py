"""The upstream clients' asyncio socket layer (ADR 0020), against a tiny real HTTP server.

What the rest of the gateway relies on: every upstream request goes through it; a stream's events
arrive as they're sent; a stalled server is a ReadTimeout; a refused connection and one closed
before any response are ConnectError and RemoteProtocolError (the two failures safe to retry,
upstream.RETRY_SAFE); and keep-alive connections are reused, unless the server closed them.
"""

import asyncio
import time

import httpx
import pytest
from tollgate_gateway.config import Settings
from tollgate_gateway.netio import AsyncioBackend, AsyncioTransport
from tollgate_gateway.upstream import _tls, make_clients

pytestmark = pytest.mark.asyncio


class RawServer:
    """An HTTP/1.1 server on 127.0.0.1 that reads each request and lets `respond` answer it.
    `respond(writer)` writes the response and returns whether to keep the connection open."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.connections = 0
        self.writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> "RawServer":
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        for writer in self.writers:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        await reader.readexactly(int(line.split(b":")[1]))
                if not await self.respond(writer):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def client(timeout: float = 5.0) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
    return httpx.AsyncClient(
        transport=AsyncioTransport(verify=_tls(), limits=limits), timeout=timeout
    )


async def ok(writer: asyncio.StreamWriter) -> bool:
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    await writer.drain()
    return True


async def test_every_upstream_request_goes_through_the_asyncio_backend(monkeypatch) -> None:
    # httpx's transport keeps its pool in a private attribute that AsyncioTransport replaces: if
    # an httpx upgrade renames it, requests would quietly go back to anyio. This catches that.
    connects = []
    real = AsyncioBackend.connect_tcp

    async def counted(self, *args, **kwargs):
        connects.append(args)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncioBackend, "connect_tcp", counted)
    async with RawServer(ok) as server:
        clients = make_clients(Settings(api_key_pepper="x", upstream_pools=2))
        try:
            for _ in range(2):  # both pools
                assert (await clients.pick().get(server.url)).text == "ok"
        finally:
            await clients.aclose()

    assert len(connects) == 2


async def test_a_streams_events_arrive_one_by_one_as_they_are_sent() -> None:
    async def sse(writer: asyncio.StreamWriter) -> bool:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")  # fmt: skip
        for i in range(3):
            event = b"data: %d\n\n" % i
            writer.write(b"%x\r\n%s\r\n" % (len(event), event))
            await writer.drain()
            await asyncio.sleep(0.05)
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        return True

    arrived = []
    async with (
        RawServer(sse) as server,
        client() as http,
        http.stream("POST", server.url, json={"stream": True}) as response,
    ):
        async for chunk in response.aiter_bytes():
            arrived.append((chunk, time.perf_counter()))

    assert [chunk for chunk, _ in arrived] == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]
    gaps = [b - a for (_, a), (_, b) in zip(arrived, arrived[1:], strict=False)]
    assert all(gap > 0.03 for gap in gaps)  # each as it came, not all at the end


async def test_a_server_that_stops_sending_is_a_read_timeout() -> None:
    async def stall(writer: asyncio.StreamWriter) -> bool:
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(10)
        return False

    async with (
        RawServer(stall) as server,
        client(timeout=0.2) as http,
        http.stream("GET", server.url) as response,
    ):
        with pytest.raises(httpx.ReadTimeout):
            async for _ in response.aiter_bytes():
                pass


async def test_a_refused_connection_is_a_connect_error() -> None:
    async with RawServer(ok) as server:
        url = server.url
    async with client() as http:  # the server is gone: nothing listens on its port
        with pytest.raises(httpx.ConnectError):
            await http.get(url)


async def test_a_connection_closed_before_any_response_is_a_remote_protocol_error() -> None:
    async def hang_up(writer: asyncio.StreamWriter) -> bool:
        return False

    async with RawServer(hang_up) as server, client() as http:
        with pytest.raises(httpx.RemoteProtocolError):
            await http.post(server.url, json={})


async def test_a_kept_alive_connection_is_reused() -> None:
    async with RawServer(ok) as server, client() as http:
        for _ in range(3):
            assert (await http.get(server.url)).text == "ok"

    assert server.connections == 1


async def test_a_connection_the_server_closed_while_idle_is_not_reused() -> None:
    async def once(writer: asyncio.StreamWriter) -> bool:
        await ok(writer)
        return False  # then close it, without saying so in the response (keep-alive expiry)

    async with RawServer(once) as server, client() as http:
        assert (await http.get(server.url)).text == "ok"
        await asyncio.sleep(0.1)  # the server's close reaches the client
        assert (await http.get(server.url)).text == "ok"  # on a new connection, no error

    assert server.connections == 2
