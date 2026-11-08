"""Billing's commands. In the cluster the database and Redis URLs are in the environment:

python -m tollgate_billing run       the consumer: usage events from Redis into the ledger
python -m tollgate_billing migrate   apply the ledger's database migrations
python -m tollgate_billing status    stream, consumer group and ledger at a glance
python -m tollgate_billing invoice   monthly invoices (ADR 0017); needs the price file:
  invoice preview <tenant> [--month YYYY-MM]   the invoice so far (default: this month)
  invoice finalize [--month YYYY-MM]           store every tenant's invoice for an ended month
                                               (default: last month), then verify it
  invoice show <tenant> <YYYY-MM>              a finalized invoice
  invoice list [--month YYYY-MM]               finalized invoices
  invoice verify [--month YYYY-MM]             every ledger row on exactly one invoice, and each
                                               invoice matches the ledger and the prices
                                               (default: last month)

kubectl -n tollgate exec deploy/tollgate-billing -- python -m tollgate_billing status
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate_billing.config import Settings
from tollgate_billing.consumer import Consumer
from tollgate_billing.db import LedgerRow, RejectRow, make_engine
from tollgate_billing.invoices import (
    FinalizeResult,
    InvoiceError,
    finalize,
    list_invoices,
    preview,
    stored_invoice,
    verify,
)
from tollgate_billing.invoicing import describe
from tollgate_billing.ledger import PostgresLedger
from tollgate_billing.migrate import upgrade
from tollgate_billing.months import Month

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


def finalize_lines(result: FinalizeResult) -> list[str]:
    out = [
        f"finalized {d.month} {d.tenant_id}: {d.currency} {d.total} for {d.requests:,} requests"
        for d in result.finalized
    ]
    if result.already:
        out.append(f"already finalized for {result.month}: {', '.join(result.already)}")
    for tenant, models in sorted(result.unpriced.items()):
        out.append(f"not finalized {result.month} {tenant}: no price for {', '.join(models)}; "
                   f"add it to prices.toml, then run finalize again")  # fmt: skip
    if not (result.finalized or result.already or result.unpriced):
        out.append(f"nothing to invoice for {result.month}: no ledger rows were recorded in it")
    return out


async def invoice(settings: Settings, args: argparse.Namespace, now: datetime) -> int:
    """The invoice commands. Returns the exit code."""
    prices = settings.require_prices()
    engine = make_engine(settings.require_database_url())
    try:
        if args.action == "preview":
            draft = await preview(engine, prices, args.tenant, args.month or Month.of(now))
            print("\n".join(describe(draft)))
            return 0
        if args.action == "show":
            found = await stored_invoice(engine, args.tenant, args.month)
            if found is None:
                print(f"no finalized invoice for {args.tenant} in {args.month}", file=sys.stderr)
                return 1
            print("\n".join(describe(found.draft, found.finalized_at)))
            return 0
        if args.action == "list":
            invoices = await list_invoices(engine, args.month)
            print(f"  {'month':<9}{'tenant':<16}{'requests':>10}{'tokens in':>14}{'tokens out':>14}"
                  f"{'total':>14}  finalized")  # fmt: skip
            for i in invoices:
                d = i.draft
                print(f"  {d.month!s:<9}{d.tenant_id:<16}{d.requests:>10,}{d.input_tokens:>14,}"
                      f"{d.output_tokens:>14,}{f'{d.currency} {i.total}':>14}"
                      f"  {i.finalized_at:%Y-%m-%d %H:%M} UTC")  # fmt: skip
            if not invoices:
                print("  no invoices")
            return 0

        # finalize and verify: last month unless told otherwise
        month = args.month or Month.of(now).previous()
        code = 0
        if args.action == "finalize":
            try:
                result = await finalize(engine, prices, month, now=now)
            except InvoiceError as e:
                print(e, file=sys.stderr)
                return 1
            print("\n".join(finalize_lines(result)))
            code = 1 if result.unpriced else 0  # a tenant left uninvoiced needs a person
        problems = await verify(engine, prices, month)
        for problem in problems:
            print(f"verify: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"verify {month}: every ledger row recorded in it is on its tenant's invoice, and "
              f"each invoice matches the ledger and the price list")  # fmt: skip
        return code
    finally:
        await engine.dispose()


def month_arg(text: str) -> Month:
    try:
        return Month.parse(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tollgate_billing", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="the consumer: usage events from Redis into the ledger")
    commands.add_parser("migrate", help="apply the database migrations")
    commands.add_parser("status", help="stream, consumer group and ledger at a glance")
    actions = commands.add_parser("invoice", help="monthly invoices").add_subparsers(
        dest="action", required=True
    )
    a = actions.add_parser("preview", help="a tenant's invoice so far (default: this month)")
    a.add_argument("tenant")
    a.add_argument("--month", type=month_arg)
    a = actions.add_parser("finalize", help="store invoices for an ended month, then verify")
    a.add_argument("--month", type=month_arg)
    a = actions.add_parser("show", help="a finalized invoice")
    a.add_argument("tenant")
    a.add_argument("month", type=month_arg)
    actions.add_parser("list", help="finalized invoices").add_argument("--month", type=month_arg)
    a = actions.add_parser("verify", help="check a month's invoices against the ledger")
    a.add_argument("--month", type=month_arg)
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if args.command == "migrate":
        upgrade(settings.require_database_url())  # synchronous: Alembic runs its own event loop
        print("schema is up to date")
    elif args.command == "status":
        asyncio.run(status(settings))
    elif args.command == "invoice":
        code = asyncio.run(invoice(settings, args, datetime.now(UTC)))
        if code:
            raise SystemExit(code)
    else:
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
