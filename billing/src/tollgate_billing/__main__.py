"""Billing's commands. In the cluster the database and Redis URLs are in the environment:

python -m tollgate_billing run       the consumer: usage events from Redis into the ledger
python -m tollgate_billing migrate   apply the ledger's database migrations
python -m tollgate_billing status    stream, consumer group and ledger at a glance

kubectl -n tollgate exec deploy/tollgate-billing -- python -m tollgate_billing status
"""

import argparse
import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate_billing.config import Settings
from tollgate_billing.consumer import Consumer
from tollgate_billing.db import LedgerRow, RejectRow, make_engine
from tollgate_billing.ledger import PostgresLedger
from tollgate_billing.migrate import upgrade

log = logging.getLogger("tollgate.billing")


def make_redis(settings: Settings) -> Redis:
    # Bytes, not decoded strings: an entry that isn't UTF-8 must reach the rejects table, not
    # crash the read. The socket timeout is well above a read's block_ms.
    return Redis.from_url(
        settings.require_redis_url(), socket_timeout=30, socket_connect_timeout=5,
        health_check_interval=30,
    )  # fmt: skip


async def run(settings: Settings) -> None:
    redis = make_redis(settings)
    engine = make_engine(settings.require_database_url())
    consumer = Consumer(
        redis,
        PostgresLedger(engine),
        stream=settings.stream,
        group=settings.group,
        name=settings.consumer,
        batch=settings.batch,
        block_ms=settings.block_ms,
        claim_idle_ms=int(settings.claim_idle_s * 1000),
        retry_s=settings.retry_s,
        retention_s=settings.retention_s,
        maintain_every_s=settings.maintain_every_s,
        stale_consumer_s=settings.stale_consumer_s,
        heartbeat=settings.heartbeat_file,
    )
    # Kubernetes stops a pod with SIGTERM: finish cleanly. (Even a hard kill is safe: anything not
    # acknowledged is read again and the ledger skips what it already has.)
    task = asyncio.current_task()
    with contextlib.suppress(NotImplementedError):  # Windows has no signal handlers in asyncio
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    log.info("consumer %s reading %s in group %s", settings.consumer, settings.stream,
             settings.group)  # fmt: skip
    try:
        await consumer.run()
    except asyncio.CancelledError:
        log.info("stopping: %d billed, %d copies skipped, %d rejected since start",
                 consumer.inserted, consumer.duplicates, consumer.rejected)  # fmt: skip
    finally:
        await redis.aclose()
        await engine.dispose()


def _when(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if value else "-"


def _entry_time(entry_id: bytes | str) -> str:
    ms = int((entry_id.decode() if isinstance(entry_id, bytes) else entry_id).split("-")[0])
    return _when(datetime.fromtimestamp(ms / 1000, UTC))


async def status_lines(redis: Redis, engine: AsyncEngine, stream: str, group: str) -> list[str]:
    lines = []
    length = await redis.xlen(stream)
    oldest = await redis.xrange(stream, count=1)
    since = f", oldest {_entry_time(oldest[0][0])}" if oldest else ""
    lines.append(f"stream {stream}: {length} entries{since}")

    groups = await redis.xinfo_groups(stream) if await redis.exists(stream) else []
    for g in groups:
        name = g["name"].decode() if isinstance(g["name"], bytes) else g["name"]
        if name != group:
            continue
        lag = g.get("lag")
        lines.append(f"group {name}: {g['pending']} pending, "
                     f"{'?' if lag is None else lag} not yet read")  # fmt: skip
        for c in await redis.xinfo_consumers(stream, name):
            cname = c["name"].decode() if isinstance(c["name"], bytes) else c["name"]
            lines.append(f"  consumer {cname}: {c['pending']} pending, idle {c['idle'] // 1000} s")
    if not any(line.startswith(f"group {group}:") for line in lines):
        lines.append(f"group {group}: not created yet (the consumer creates it)")

    async with engine.connect() as conn:
        rows, last = (
            await conn.execute(select(func.count(), func.max(LedgerRow.recorded_at)))
        ).one()
        rejects = (await conn.execute(select(func.count()).select_from(RejectRow))).scalar_one()
        per_tenant = (
            await conn.execute(
                select(
                    LedgerRow.tenant_id,
                    func.count(),
                    func.sum(LedgerRow.billable_input_tokens),
                    func.sum(LedgerRow.billable_output_tokens),
                    func.sum(LedgerRow.input_tokens),
                    func.sum(LedgerRow.output_tokens),
                )
                .group_by(LedgerRow.tenant_id)
                .order_by(LedgerRow.tenant_id)
            )
        ).all()
    lines.append(f"ledger: {rows} rows, last recorded {_when(last)}; rejects: {rejects}")
    if per_tenant:
        lines.append(f"  {'tenant':<14}{'requests':>9}{'billed in':>11}{'billed out':>12}"
                     f"{'measured in':>13}{'measured out':>14}")  # fmt: skip
        for tenant, count, b_in, b_out, m_in, m_out in per_tenant:
            lines.append(f"  {tenant:<14}{count:>9}{b_in:>11}{b_out:>12}{m_in:>13}{m_out:>14}")
    return lines


async def status(settings: Settings) -> None:
    redis = make_redis(settings)
    engine = make_engine(settings.require_database_url())
    try:
        print("\n".join(await status_lines(redis, engine, settings.stream, settings.group)))
    finally:
        await redis.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m tollgate_billing", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument("command", choices=["run", "migrate", "status"])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if args.command == "migrate":
        upgrade(settings.require_database_url())  # synchronous: Alembic runs its own event loop
        print("schema is up to date")
    elif args.command == "status":
        asyncio.run(status(settings))
    else:
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
