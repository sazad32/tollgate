"""Waiting for a local slot, around one request's life (ADR 0014).

The fair queue itself is in queue.py. This module connects it to a request:
- wait_for_slot() before a local backend is called: join the tenant's queue, then check every
  `poll_s` seconds whether it's this request's turn;
- a full queue, or a wait longer than `max_wait_s`, becomes a 429 with a Retry-After estimate;
- a client that hangs up while waiting gives up its place;
- Ticket.release() when the request ends, however it ends;
- a background loop renews the leases of everything this replica holds, so a replica that dies
  stops renewing and its slots come back;
- if Redis is down, requests go ahead without waiting (fail open), like rate limits (ADR 0012).

.NET: wait_for_slot() is `await semaphore.WaitAsync(timeout)`, with the semaphore in Redis.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio
from fastapi.responses import JSONResponse

from tollgate_gateway.errors import openai_error
from tollgate_gateway.queue import FairQueue, retry_after_s

log = logging.getLogger("tollgate.gateway.admission")


@dataclass(frozen=True)
class QueueRefusal:
    code: str  # queue_full | queue_timeout
    message: str
    retry_after_s: int


@dataclass
class Ticket:
    """A slot this request holds. Release it exactly once, when the request ends."""

    admission: "Admission"
    tenant: str
    request_id: str
    started: float
    backend: str | None = None  # the backend its slot is on; None when not queued
    queued: bool = True  # False: Redis was down and the request went ahead without a slot
    released: bool = field(default=False)

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.queued:
            await self.admission.release(self)


def queue_refused(refusal: QueueRefusal) -> JSONResponse:
    response = openai_error(429, refusal.message, "requests", refusal.code)
    response.headers["Retry-After"] = str(refusal.retry_after_s)
    return response


class Admission:
    def __init__(
        self,
        queue: FairQueue,
        clock: Callable[[], float],
        *,
        max_wait_s: float = 120.0,
        poll_s: float = 0.1,
        initial_hold_s: float = 10.0,
        max_hold_s: float = 1800.0,
        on_fail_open: Callable[[str], None] | None = None,
        retry_release_s: float = 0.2,
    ) -> None:
        self.queue = queue
        self.clock = clock
        self.max_wait_s = max_wait_s
        self.poll_s = poll_s
        # How long a request holds a slot, on average (moving average). Feeds Retry-After.
        self.avg_hold_s = initial_hold_s
        # Nothing legitimately holds a slot this long (the longest generation takes ~15 min on a
        # node). A ticket whose release never ran (a bug, or a stream that never started) stops
        # being renewed then, and its lease runs out: a backstop against leaking slots for good.
        self.max_hold_s = max_hold_s
        # (tenant, request id) -> when it joined: everything waiting or running on this replica.
        self.held: dict[tuple[str, str], float] = {}
        # Told each time Redis fails and a step goes ahead without it (the gateway's metrics).
        self._on_fail_open = on_fail_open or (lambda step: None)
        # Slots whose release failed, being retried (ADR 0020). Without this a slot stays taken
        # until its lease runs out: 30 s of a GPU blocked, on a backend with 2 slots.
        self.retry_release_s = retry_release_s
        self._retries: set[asyncio.Task] = set()

    @property
    def pending_releases(self) -> int:
        return len(self._retries)

    def cancel_pending(self) -> None:
        for task in self._retries:
            task.cancel()

    def _open(self, tenant: str, request_id: str) -> Ticket:
        return Ticket(self, tenant, request_id, self.clock(), queued=False)

    async def wait_for_slot(
        self,
        tenant: str,
        request_id: str,
        backends: Callable[[], list[tuple[str, int, int]]],
        disconnected: Callable[[], Awaitable[bool]],
    ) -> Ticket | QueueRefusal | None:
        """A Ticket once it's this request's turn, naming the backend its slot is on; a
        QueueRefusal (send 429); or None if the client hung up while waiting.

        backends: the ones that are up right now, as (name, slots, priority)."""

        def capacity() -> int:
            return sum(slots for _, slots, _ in backends())

        key = (tenant, request_id)
        try:
            joined = await self.queue.join(tenant, request_id)
        except Exception as exc:  # fail open: no queue rather than no service
            log.warning("queue skipped for %s, Redis unavailable: %r", tenant, exc)
            self._on_fail_open("queue")
            return self._open(tenant, request_id)
        if not joined.accepted:
            wait = retry_after_s(joined.waiting_total, capacity(), self.avg_hold_s)
            return QueueRefusal(
                "queue_full",
                f"Too many requests waiting: your tenant already has {joined.waiting} queued. "
                f"Try again in {wait} s.",
                wait,
            )

        self.held[key] = self.clock()
        admitted = False
        deadline = self.clock() + self.max_wait_s
        try:
            while True:
                turn = await self.queue.try_start(tenant, request_id, backends())
                if turn.outcome == "start":
                    admitted = True
                    return Ticket(self, tenant, request_id, self.clock(), turn.backend)
                if turn.outcome == "gone":  # its lease ran out (Redis was unreachable for a while)
                    await self.queue.join(tenant, request_id)
                if self.clock() >= deadline:
                    state = await self.queue.state()
                    waiting = sum(state.waiting.values())
                    wait = retry_after_s(waiting, capacity(), self.avg_hold_s)
                    return QueueRefusal(
                        "queue_timeout",
                        f"Waited {self.max_wait_s:.0f} s for a free model server without getting "
                        f"one. Try again in {wait} s.",
                        wait,
                    )
                if await disconnected():
                    log.info("request %s: client left while queued", request_id)
                    return None
                await asyncio.sleep(self.poll_s)
        except Exception as exc:  # Redis failed mid-wait: go ahead (fail open)
            log.warning("queue skipped for %s, Redis unavailable: %r", tenant, exc)
            self._on_fail_open("queue")
            return self._open(tenant, request_id)
        finally:
            if not admitted:
                self.held.pop(key, None)
                # Shielded: a cancelled request must still leave the queue, or it'd block others
                # until its lease ran out.
                with anyio.CancelScope(shield=True):
                    await self._leave(tenant, request_id)

    async def release(self, ticket: Ticket) -> None:
        self.held.pop((ticket.tenant, ticket.request_id), None)
        held_for = max(0.0, self.clock() - ticket.started)
        self.avg_hold_s = 0.8 * self.avg_hold_s + 0.2 * held_for
        await self._leave(ticket.tenant, ticket.request_id)

    async def _leave(self, tenant: str, request_id: str) -> None:
        try:
            await self.queue.finish(tenant, request_id)
        except Exception as exc:
            log.warning("could not leave the queue for %s, retrying: %r", request_id, exc)
            self._on_fail_open("queue_release")
            self._retry_leave(tenant, request_id)

    def _retry_leave(self, tenant: str, request_id: str) -> None:
        """Keep trying to free the slot, backing off, until it's freed or its lease would have
        freed it anyway."""
        give_up_at = self.clock() + self.queue.lease_s

        async def retry() -> None:
            delay = self.retry_release_s
            while self.clock() < give_up_at:
                await asyncio.sleep(delay)
                try:
                    await self.queue.finish(tenant, request_id)
                    return
                except Exception:
                    delay = min(delay * 2, 2.0)

        task = asyncio.create_task(retry())
        self._retries.add(task)
        task.add_done_callback(self._retries.discard)

    async def run_renewals(self) -> None:
        """Renew this replica's leases every third of a lease, until cancelled at shutdown."""
        while True:
            await asyncio.sleep(self.queue.lease_s / 3)
            now = self.clock()
            for key, since in list(self.held.items()):
                if now - since > self.max_hold_s:
                    log.error("request %s held a queue slot for %.0f s: no longer renewed",
                              key[1], now - since)  # fmt: skip
                    del self.held[key]
            try:
                await self.queue.renew(list(self.held))
            except Exception as exc:
                log.warning("could not renew queue leases: %r", exc)
                self._on_fail_open("queue_renew")
