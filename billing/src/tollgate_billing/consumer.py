"""The billing consumer: usage events from the Redis stream to the ledger, exactly once (ADR 0016).

It reads `tollgate:usage` as a member of the consumer group `billing`. Redis remembers, per group,
which entries were handed to which consumer and not yet acknowledged (the "pending" list), so:

1. Read a batch: first our own pending entries (a batch whose write failed), then entries a dead
   consumer left pending for over a minute (XAUTOCLAIM), then new ones (waiting up to 5 s).
2. Write it to the ledger in one transaction (ledger.py). Duplicates are skipped by the key.
3. Acknowledge it (XACK), only after the commit.

A crash between 2 and 3 means the batch is read and written again, and the ledger skips every row:
at-least-once delivery plus an idempotent write gives each event exactly once in the ledger.

Every minute it also trims the stream (acknowledged entries older than the replay window) and
forgets consumers of pods that are gone.

.NET: a BackgroundService reading a Service Bus subscription with peek-lock. Pending = locked,
XACK = Complete(), XAUTOCLAIM = the lock expiring and another receiver taking the message.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from tollgate_billing.events import InvalidEventError, parse, raw_fields
from tollgate_billing.ledger import Entry, Ledger, Reject

log = logging.getLogger("tollgate.billing.consumer")


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _id(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


@dataclass(frozen=True)
class GroupPosition:
    """How far one consumer group has got: the last entry it was handed, and the oldest entry it
    was handed but hasn't acknowledged (None when it owes nothing)."""

    last_delivered: str
    oldest_pending: str | None


def trim_point(groups: Sequence[GroupPosition], now_ms: int, retention_ms: int) -> str | None:
    """The lowest stream ID to keep (XTRIM MINID), or None to keep everything.

    An entry may go only if every group has acknowledged it (handed out and not pending) and it's
    older than the replay window. Stream IDs start with their time in milliseconds, so the window
    is just another ID.
    """
    if not groups:
        return None
    keep_from = (now_ms - retention_ms, 0)
    for g in groups:
        if g.oldest_pending is not None:
            limit = _id(g.oldest_pending)
        else:
            ms, seq = _id(g.last_delivered)
            limit = (ms, seq + 1)  # the first entry after the last one handed out
        keep_from = min(keep_from, limit)
    if keep_from <= (0, 1):
        return None
    return f"{keep_from[0]}-{keep_from[1]}"


class Consumer:
    def __init__(
        self,
        redis: Redis,
        ledger: Ledger,
        *,
        stream: str = "tollgate:usage",
        group: str = "billing",
        name: str,
        batch: int = 100,
        block_ms: int = 5000,
        claim_idle_ms: int = 60_000,
        retry_s: float = 5.0,
        retention_s: float = 86_400.0,
        maintain_every_s: float = 60.0,
        stale_consumer_s: float = 3600.0,
        heartbeat: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.ledger = ledger
        self.stream = stream
        self.group = group
        self.name = name
        self.batch = batch
        self.block_ms = block_ms
        self.claim_idle_ms = claim_idle_ms
        self.retry_s = retry_s
        self.retention_s = retention_s
        self.maintain_every_s = maintain_every_s
        self.stale_consumer_s = stale_consumer_s
        self.heartbeat = heartbeat
        self.clock = clock
        self._group_ready = False
        # Counters since start, for the logs.
        self.inserted = 0
        self.duplicates = 0
        self.rejected = 0
        self.gone = 0

    # --- reading -------------------------------------------------------------------------------

    async def ensure_group(self) -> None:
        """Create the group if it's missing, reading from the stream's first entry ("0"), so events
        written before billing ever ran are billed too. MKSTREAM: create the stream if need be."""
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            log.info("created consumer group %s on %s", self.group, self.stream)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # BUSYGROUP: it exists already, which is fine
                raise
        self._group_ready = True

    async def _read(self, start: str, block_ms: int | None) -> list:
        # start "0": our own pending entries. ">": entries never handed to anyone.
        reply = await self.redis.xreadgroup(
            self.group, self.name, {self.stream: start}, count=self.batch, block=block_ms
        )
        return reply[0][1] if reply else []

    async def _take_over(self) -> list:
        """Entries another consumer was handed but hasn't acknowledged for claim_idle_ms: its pod
        died. They move to us. (Entries deleted meanwhile are dropped from the pending list.)"""
        _, entries, _ = await self.redis.xautoclaim(
            self.stream, self.group, self.name, self.claim_idle_ms, start_id="0-0", count=self.batch
        )
        if entries:
            log.warning("took over %d usage events from a consumer that stopped", len(entries))
        return entries

    async def step(self) -> int:
        """Read one batch, write it, acknowledge it. Returns how many entries it handled.
        Raises if the ledger or Redis failed; nothing unwritten was acknowledged."""
        if not self._group_ready:
            await self.ensure_group()
        try:
            entries = await self._read("0", None)
            if not entries:
                entries = await self._take_over()
            if not entries:
                entries = await self._read(">", self.block_ms)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):  # the stream (and its group) was deleted: start again
                self._group_ready = False
            raise
        if entries:
            await self._handle(entries)
        return len(entries)

    # --- writing -------------------------------------------------------------------------------

    async def _handle(self, entries: list) -> None:
        accepted: list[Entry] = []
        rejects: list[Reject] = []
        ids: list[str] = []
        gone = 0
        for raw_id, fields in entries:
            stream_id = _text(raw_id)
            ids.append(stream_id)
            if not fields:
                # Deleted from the stream after it was handed out: there's nothing left to bill.
                gone += 1
                log.warning("usage event %s was deleted before billing could read it", stream_id)
                continue
            try:
                accepted.append(Entry(stream_id, parse(fields)))
            except InvalidEventError as exc:
                rejects.append(Reject(stream_id, raw_fields(fields), str(exc)))
                log.warning("usage event %s rejected: %s", stream_id, exc)

        result = await self.ledger.write(accepted, rejects)
        self.inserted += result.inserted
        self.duplicates += result.duplicates
        self.rejected += result.rejected
        self.gone += gone
        # Only now, with the batch committed. If this fails, the batch comes round again and the
        # ledger skips it.
        await self.redis.xack(self.stream, self.group, *ids)
        log.info("billed %d usage events (%d copies skipped, %d rejected)",
                 result.inserted, result.duplicates, result.rejected)  # fmt: skip

    # --- housekeeping --------------------------------------------------------------------------

    async def _positions(self) -> list[GroupPosition]:
        positions = []
        for info in await self.redis.xinfo_groups(self.stream):
            name = _text(info["name"])
            oldest = None
            if info["pending"]:
                summary = await self.redis.xpending(self.stream, name)
                oldest = _text(summary["min"]) if summary["min"] else None
            positions.append(GroupPosition(_text(info["last-delivered-id"]), oldest))
        return positions

    async def maintain(self) -> None:
        """Trim what every group has acknowledged and the replay window has passed, and forget
        consumers (old pod names) that owe nothing and haven't read for stale_consumer_s."""
        now_ms = int(self.clock() * 1000)
        point = trim_point(await self._positions(), now_ms, int(self.retention_s * 1000))
        if point is not None:
            removed = await self.redis.xtrim(self.stream, minid=point, approximate=False)
            if removed:
                log.info("trimmed %d billed usage events from %s", removed, self.stream)

        for c in await self.redis.xinfo_consumers(self.stream, self.group):
            name = _text(c["name"])
            if (
                name != self.name
                and c["pending"] == 0
                and c["idle"] >= self.stale_consumer_s * 1000
            ):
                await self.redis.xgroup_delconsumer(self.stream, self.group, name)
                log.info("forgot consumer %s (gone, owed nothing)", name)

    # --- the loop ------------------------------------------------------------------------------

    def _beat(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.touch()

    async def run(self) -> None:
        """Until cancelled. Failures are waited out: nothing is acknowledged until it's written,
        so a Postgres or Redis outage only delays billing."""
        failing = False
        next_maintenance = 0.0
        while True:
            self._beat()  # liveness: the loop goes round at least every block_ms or retry_s
            # Let other tasks run even if a read returns at once. Real Redis waits up to block_ms
            # for new entries; fakeredis answers an empty read immediately.
            await asyncio.sleep(0)
            try:
                await self.step()
                if time.monotonic() >= next_maintenance:
                    await self.maintain()
                    next_maintenance = time.monotonic() + self.maintain_every_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not failing:
                    log.warning("billing paused, will retry every %.0f s: %r", self.retry_s, exc)
                failing = True
                await asyncio.sleep(self.retry_s)
                continue
            if failing:
                log.info("billing running again")
                failing = False
