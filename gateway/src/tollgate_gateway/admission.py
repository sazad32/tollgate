"""Waiting for a local slot, around one request's life (ADR 0014).

The fair queue itself is in queue.py. This module connects it to a request:
- first_trip() before a local backend is called: reserve the request's estimate, join the
  tenant's queue and take a free slot if it's this request's turn, in one Redis call (ADMIT,
  admit.py, ADR 0021);
- wait_turn() if there was no free slot: check every `poll_s` seconds whether it's its turn;
- wait_for_slot(), the same without the reservation: join, then wait_turn();
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

from tollgate_gateway.admit import FirstTrip, OneTrip
from tollgate_gateway.errors import openai_error
from tollgate_gateway.limits import Limit, TokenBuckets
from tollgate_gateway.queue import FairQueue, Joined, retry_after_s

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
        buckets: TokenBuckets | None = None,
    ) -> None:
        self.queue = queue
        # Reserve, join and start in one Redis call (ADR 0021): the token buckets share the Redis.
        self.one_trip = OneTrip(buckets, queue) if buckets is not None else None
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

    def open_ticket(self, tenant: str, request_id: str) -> Ticket:
        """For a request that goes ahead without a slot: Redis was down (fail open)."""
        return Ticket(self, tenant, request_id, self.clock(), queued=False)

    def ticket(self, tenant: str, request_id: str, backend: str | None) -> Ticket:
        """For a request first_trip() started: its slot is on `backend`."""
        return Ticket(self, tenant, request_id, self.clock(), backend)

    def full_queue(self, joined: Joined, backends: list[tuple[str, int, int]]) -> QueueRefusal:
        capacity = sum(slots for _, slots, _ in backends)
        wait = retry_after_s(joined.waiting_total, capacity, self.avg_hold_s)
        return QueueRefusal(
            "queue_full",
            f"Too many requests waiting: your tenant already has {joined.waiting} queued. "
            f"Try again in {wait} s.",
            wait,
        )

    async def first_trip(
        self,
        tenant: str,
        request_id: str,
        limit: Limit,
        tokens: int,
        backends: list[tuple[str, int, int]],
    ) -> FirstTrip | None:
        """Reserve `tokens`, join the queue and start if a slot is free and it's this request's
        turn: one Redis call (ADR 0021). None if Redis failed: the request goes ahead with no limit
        and no queue (fail open), and both steps are counted.

        After "start", the request holds a slot: ticket(). After "wait", it's in the queue with its
        reservation: wait_turn(). "limited" and "queue_full" left nothing behind in Redis.
        backends: the ones that are up, as (name, slots, priority)."""
        assert self.one_trip is not None, "first_trip needs the token buckets"
        try:
            first = await self.one_trip.admit(tenant, request_id, limit, tokens, backends)
        except Exception as exc:  # fail open: no limits and no queue, rather than no service
            log.warning("rate limits and queue skipped for %s, Redis unavailable: %r", tenant, exc)
            self._on_fail_open("rate_limit")
            self._on_fail_open("queue")
            return None
        if first.outcome in ("start", "wait"):
            self.held[(tenant, request_id)] = self.clock()  # its lease is renewed from now on
        return first

    async def wait_for_slot(
        self,
        tenant: str,
        request_id: str,
        backends: Callable[[], list[tuple[str, int, int]]],
        disconnected: Callable[[], Awaitable[bool]],
    ) -> Ticket | QueueRefusal | None:
        """A Ticket once it's this request's turn, naming the backend its slot is on; a
        QueueRefusal (send 429); or None if the client hung up while waiting. For a request with
        no reservation to make in the same call (first_trip() does both).

        backends: the ones that are up right now, as (name, slots, priority)."""
        try:
            joined = await self.queue.join(tenant, request_id)
        except Exception as exc:  # fail open: no queue rather than no service
            log.warning("queue skipped for %s, Redis unavailable: %r", tenant, exc)
            self._on_fail_open("queue")
            return self.open_ticket(tenant, request_id)
        if not joined.accepted:
            return self.full_queue(joined, backends())
        self.held[(tenant, request_id)] = self.clock()
        return await self.wait_turn(tenant, request_id, backends, disconnected, tried=False)

    async def wait_turn(
        self,
        tenant: str,
        request_id: str,
        backends: Callable[[], list[tuple[str, int, int]]],
        disconnected: Callable[[], Awaitable[bool]],
        *,
        tried: bool = True,
    ) -> Ticket | QueueRefusal | None:
        """For a request already in the queue: try every `poll_s` seconds whether it's its turn.
        A Ticket when it is; a QueueRefusal after max_wait_s; None if the client hung up.
        tried: it was tried just now (first_trip() does), so wait a poll before the next try."""
        key = (tenant, request_id)
        admitted = False
        deadline = self.clock() + self.max_wait_s
        try:
            while True:
                if tried:
                    if self.clock() >= deadline:
                        state = await self.queue.state()
                        waiting = sum(state.waiting.values())
                        capacity = sum(slots for _, slots, _ in backends())
                        wait = retry_after_s(waiting, capacity, self.avg_hold_s)
                        return QueueRefusal(
                            "queue_timeout",
                            f"Waited {self.max_wait_s:.0f} s for a free model server without "
                            f"getting one. Try again in {wait} s.",
                            wait,
                        )
                    if await disconnected():
                        log.info("request %s: client left while queued", request_id)
                        return None
                    await asyncio.sleep(self.poll_s)
                tried = True
                turn = await self.queue.try_start(tenant, request_id, backends())
                if turn.outcome == "start":
                    admitted = True
                    return Ticket(self, tenant, request_id, self.clock(), turn.backend)
                if turn.outcome == "gone":  # its lease ran out (Redis was unreachable for a while)
                    await self.queue.join(tenant, request_id)
        except Exception as exc:  # Redis failed mid-wait: go ahead (fail open)
            log.warning("queue skipped for %s, Redis unavailable: %r", tenant, exc)
            self._on_fail_open("queue")
            return self.open_ticket(tenant, request_id)
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
