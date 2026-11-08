"""Metering check (ADR 0018, the Phase 3 gate): a mix of requests through the gateway, and what each
client saw, keyed by the gateway's request ID (the X-Request-ID header).

Every tenant sends its share: half plain requests, four in ten streams, and one in ten streams the
client hangs up on after a few chunks. The token counts recorded are the server's usage as the
gateway passed it on, never counted here. `python -m tollgate_billing check-requests` then audits
each record against its ledger row.

.NET: like N tasks per tenant pulling from a ConcurrentQueue<string> of request kinds, all awaited
with Task.WhenAll.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from tollgate_bench.client import REQUEST_TIMEOUT
from tollgate_bench.sse import has_content, parse_line

MAX_TOKENS = 32  # short answers: the check is about counting, not speed
HANG_UP_AFTER = 5  # content chunks a hanging-up client reads first


@dataclass(frozen=True)
class MeteredRequest:
    request_id: str | None  # the gateway's X-Request-ID; None if no response came back
    tenant: str
    kind: str  # plain | stream | hang-up
    outcome: str  # completed | hung_up | error
    status_code: int | None
    input_tokens: int | None  # the usage the client was told; None if it never got it
    output_tokens: int | None
    chunks: int  # content chunks received (streams)
    error: str | None


def mix(requests: int) -> list[str]:
    """Every tenth request is hung up on; of the rest, every other one is streamed."""
    return ["hang-up" if i % 10 == 9 else "stream" if i % 2 == 1 else "plain"
            for i in range(requests)]  # fmt: skip


def request_body(model: str, label: str, max_tokens: int, stream: bool) -> dict:
    body = {
        "model": model,
        # Synthetic, and unique per request, so no answer comes from a prompt cache.
        "messages": [{"role": "user", "content": f"Request {label}. In one sentence, why do "
                                                 f"prepaid customers check their balance?"}],
        "max_tokens": max_tokens,
        "ignore_eos": True,  # exactly max_tokens out, every time
        "temperature": 0,
        "stream": stream,
    }  # fmt: skip
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


async def one(client: httpx.AsyncClient, url: str, tenant: str, key: str, kind: str, label: str,
              model: str, max_tokens: int, hang_up_after: int) -> MeteredRequest:  # fmt: skip
    """Send one request. Never raises: a failure is a record with outcome "error"."""
    headers = {"Authorization": f"Bearer {key}"}
    body = request_body(model, label, max_tokens, stream=kind != "plain")
    request_id = status = None
    try:
        if kind == "plain":
            response = await client.post(url, json=body, headers=headers)
            request_id, status = response.headers.get("x-request-id"), response.status_code
            if status != 200:
                return MeteredRequest(request_id, tenant, kind, "error", status, None, None, 0,
                                      f"HTTP {status}: {response.text[:200]}")  # fmt: skip
            usage = response.json()["usage"]
            return MeteredRequest(
                request_id,
                tenant,
                kind,
                "completed",
                status,
                usage["prompt_tokens"],
                usage["completion_tokens"],
                0,
                None,
            )
        async with client.stream("POST", url, json=body, headers=headers) as response:
            request_id, status = response.headers.get("x-request-id"), response.status_code
            if status != 200:
                text = (await response.aread()).decode(errors="replace")[:200]
                return MeteredRequest(request_id, tenant, kind, "error", status, None, None, 0,
                                      f"HTTP {status}: {text}")  # fmt: skip
            chunks, usage = 0, None
            async for line in response.aiter_lines():
                chunk = parse_line(line)
                if chunk is None:
                    continue
                if has_content(chunk):
                    chunks += 1
                    if kind == "hang-up" and chunks >= hang_up_after:
                        # Leaving the `async with` closes the connection: the gateway sees a
                        # client that went away.
                        return MeteredRequest(request_id, tenant, kind, "hung_up", status, None,
                                              None, chunks, None)  # fmt: skip
                if chunk.get("usage"):
                    usage = chunk["usage"]
        tokens = (usage["prompt_tokens"], usage["completion_tokens"]) if usage else (None, None)
        return MeteredRequest(request_id, tenant, kind, "completed", status, *tokens, chunks, None)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return MeteredRequest(request_id, tenant, kind, "error", status, None, None, 0,
                              f"{type(e).__name__}: {e}")  # fmt: skip


async def run_metering(base_url: str, model: str, keys: dict[str, str], *, requests: int,
                       concurrency: int, max_tokens: int = MAX_TOKENS,
                       hang_up_after: int = HANG_UP_AFTER, run_id: str = "metering",
                       transport: httpx.AsyncBaseTransport | None = None,
                       ) -> list[MeteredRequest]:  # fmt: skip
    """`requests` per tenant, `concurrency` at a time per tenant, all tenants at once."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    records: list[MeteredRequest] = []
    total = len(keys) * concurrency
    limits = httpx.Limits(max_connections=total, max_keepalive_connections=total)

    async def tenant_worker(client: httpx.AsyncClient, tenant: str, queue: list[tuple[int, str]]):
        while queue:
            index, kind = queue.pop(0)  # no await in between: safe without a lock
            label = f"{run_id}-{tenant}-{index:04d}"
            records.append(await one(client, url, tenant, keys[tenant], kind, label, model,
                                     max_tokens, hang_up_after))  # fmt: skip

    async with (
        httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=limits, transport=transport) as client,
        asyncio.TaskGroup() as group,
    ):
        for tenant in keys:
            queue = list(enumerate(mix(requests)))
            for _ in range(concurrency):
                group.create_task(tenant_worker(client, tenant, queue))
    return records


def summary(records: Sequence[MeteredRequest]) -> list[str]:
    out = []
    for tenant in sorted({r.tenant for r in records}):
        mine = [r for r in records if r.tenant == tenant]
        count = {o: sum(r.outcome == o for r in mine) for o in ("completed", "hung_up", "error")}
        out.append(f"{tenant}: {len(mine)} requests: {count['completed']} completed, "
                   f"{count['hung_up']} hung up, {count['error']} errors")  # fmt: skip
    return out
