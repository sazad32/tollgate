"""The local model servers, and how busy each one is right now (ADR 0013).

Each backend's own /metrics says how many requests it is running and how many are waiting. The
gateway reads them every second, so both gateway replicas route on the same numbers. (Counters kept
inside each replica would see only half the traffic.)

.NET: LoadTracker.run() is a BackgroundService: a loop on a PeriodicTimer, started with the app
(main.py, lifespan) and cancelled when it stops.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from tollgate_gateway.router import BackendLoad

log = logging.getLogger("tollgate.gateway.backends")

# Which metrics mean "running" and "waiting" on each kind of server.
LOAD_METRICS = {
    "llama": ("llamacpp:requests_processing", "llamacpp:requests_deferred"),
    "vllm": ("vllm:num_requests_running", "vllm:num_requests_waiting"),
}


class Backend(BaseModel):
    """One local model server, from TOLLGATE_BACKENDS (a JSON list of these)."""

    name: str  # shown to clients in the x-tollgate-backend header
    url: str  # base URL, e.g. http://llama-server-0.llama-server-headless.tollgate.svc:8080
    kind: Literal["llama", "vllm"] = "llama"  # how to read its /metrics
    slots: int = Field(default=2, ge=1)  # requests it serves at once (llama-server's --parallel)
    priority: int = 0  # lower is tried first while it has a free slot


def parse_load(kind: str, text: str) -> int:
    """Running + waiting requests, from Prometheus text. Missing metrics count as 0.

    A line looks like `name{label="x"} 7.0`. Names match exactly; values are summed over labels.
    """
    wanted = LOAD_METRICS[kind]
    total = 0.0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name_part, _, value = line.rpartition(" ")
        name = name_part.split("{", 1)[0].strip()
        if name in wanted:
            try:
                total += float(value)
            except ValueError:
                continue
    return round(total)


@dataclass
class _State:
    up: bool = True  # until a poll says otherwise: the first requests don't wait for a poll
    busy: int = 0  # running + waiting, at the last poll
    sent: int = 0  # requests this replica sent there since the last poll


class LoadTracker:
    def __init__(
        self, backends: list[Backend], client: httpx.AsyncClient, timeout_s: float = 1.0
    ) -> None:
        self.backends = {b.name: b for b in backends}
        self.client = client
        self.timeout_s = timeout_s
        self._state = {name: _State() for name in self.backends}

    def backend(self, name: str) -> Backend:
        return self.backends[name]

    def snapshot(self) -> list[BackendLoad]:
        return [
            BackendLoad(
                name=name,
                up=state.up,
                busy=state.busy + state.sent,
                slots=self.backends[name].slots,
                priority=self.backends[name].priority,
            )
            for name, state in self._state.items()
        ]

    def sent(self, name: str) -> None:
        """Count a request sent there, so the next request sees it before the next poll does."""
        self._state[name].sent += 1

    def mark_down(self, name: str) -> None:
        """A request couldn't connect. Skip the backend until a poll finds it again."""
        self._state[name].up = False

    async def poll_once(self) -> None:
        await asyncio.gather(*(self._poll(name) for name in self.backends))

    async def _poll(self, name: str) -> None:
        backend, state = self.backends[name], self._state[name]
        sent_before = state.sent  # requests sent while the poll is in flight are kept
        try:
            response = await self.client.get(backend.url + "/metrics", timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            if state.up:
                log.warning("backend %s is down: %r", name, exc)
            state.up = False
            return
        if response.status_code != 200:
            if state.up:
                log.warning("backend %s is down: /metrics answered %s", name, response.status_code)
            state.up = False
            return
        if not state.up:
            log.info("backend %s is back", name)
        state.up = True
        state.busy = parse_load(backend.kind, response.text)
        state.sent -= sent_before

    async def run(self, interval_s: float) -> None:
        """Poll forever, every interval_s seconds, until cancelled at shutdown. One loop per
        backend, so one that times out (the workstation switched off) doesn't delay the others."""

        async def loop(name: str) -> None:
            while True:
                await self._poll(name)
                await asyncio.sleep(interval_s)

        await asyncio.gather(*(loop(name) for name in self.backends))
