"""The upstream clients' socket layer: plain asyncio streams instead of anyio (ADR 0020).

httpcore reads a streamed response one socket read per token, and its default backend wraps
every read in an anyio cancel scope for the read timeout. Profiled under load (week 15), that
wrapper alone was 12% of the gateway's CPU. This backend does the same job with asyncio's own
StreamReader and `asyncio.timeout`. httpcore's pool, HTTP/1.1 parsing and error handling stay
as they are: only the bytes in and out change hands here.

.NET: like giving SocketsHttpHandler a custom ConnectCallback that returns your own Stream.
"""

import asyncio
import select
import socket
import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

# httpcore's interface passes each call's timeout as a parameter: it can't be asyncio.timeout
# around the call, as the rule asks.
# ruff: noqa: ASYNC109

SocketOption = tuple[int, int, int | bytes] | tuple[int, int, None, int]


class AsyncioStream(httpcore.AsyncNetworkStream):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            async with asyncio.timeout(timeout):
                return await self._reader.read(max_bytes)
        except TimeoutError as exc:  # before OSError: TimeoutError is one
            raise httpcore.ReadTimeout(exc) from exc
        except OSError as exc:
            raise httpcore.ReadError(exc) from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        try:
            async with asyncio.timeout(timeout):
                self._writer.write(buffer)
                await self._writer.drain()
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(exc) from exc
        except OSError as exc:
            raise httpcore.WriteError(exc) from exc

    async def aclose(self) -> None:
        self._writer.close()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):
                await self._writer.start_tls(ssl_context, server_hostname=server_hostname)
        except TimeoutError as exc:
            self._writer.close()
            raise httpcore.ConnectTimeout(exc) from exc
        except (OSError, ssl.SSLError) as exc:
            self._writer.close()
            raise httpcore.ConnectError(exc) from exc
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "ssl_object":
            return self._writer.get_extra_info("ssl_object")
        if info == "client_addr":
            return self._writer.get_extra_info("sockname")
        if info == "server_addr":
            return self._writer.get_extra_info("peername")
        if info == "socket":
            return self._writer.get_extra_info("socket")
        if info == "is_readable":
            # Asked of an idle keep-alive connection before reusing it: anything to read (the
            # server closed it, or sent something unasked) means it can't be reused.
            return self._reader.at_eof() or _readable(self._writer.get_extra_info("socket"))
        return None


def _readable(sock: Any) -> bool:
    if sock is None or sock.fileno() < 0:
        return True
    if not hasattr(select, "poll"):  # Windows, local development only
        return bool(select.select([sock], [], [], 0)[0])
    poll = select.poll()  # not select.select: it fails past file descriptor 1024
    poll.register(sock.fileno(), select.POLLIN)
    return bool(poll.poll(0))


class AsyncioBackend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        local = (local_address, 0) if local_address else None
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(host, port, local_addr=local)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(exc) from exc
        except OSError as exc:
            raise httpcore.ConnectError(exc) from exc
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # small writes, sent at once
        for option in socket_options or ():
            sock.setsockopt(*option)
        return AsyncioStream(reader, writer)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_unix_connection(path)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(exc) from exc
        except OSError as exc:
            raise httpcore.ConnectError(exc) from exc
        return AsyncioStream(reader, writer)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class AsyncioTransport(httpx.AsyncHTTPTransport):
    """httpx's own transport, with its connection pool rebuilt on AsyncioBackend.

    Gotcha: `_pool` is httpx's private attribute (httpx 0.28). test_netio.py checks that requests
    really go through AsyncioBackend, so an httpx upgrade that renames it fails a test instead of
    silently falling back to anyio.
    """

    def __init__(self, *, verify: ssl.SSLContext, limits: httpx.Limits) -> None:
        super().__init__(verify=verify, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=verify,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=AsyncioBackend(),
        )
