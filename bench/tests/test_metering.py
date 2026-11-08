"""The metering check's client (ADR 0018) against a fake gateway (httpx.MockTransport)."""

import itertools
import json

import httpx
import pytest
from tollgate_bench.metering import MeteredRequest, mix, run_metering, summary


class FakeGateway:
    """Answers like the gateway: an X-Request-ID on every response, usage from the 'server'."""

    def __init__(self, refuse_every: int = 0) -> None:
        self.ids = (f"{n:032x}" for n in itertools.count(1))
        self.calls = 0
        self.refuse_every = refuse_every
        self.bodies: list[dict] = []
        self.keys: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        self.bodies.append(body)
        self.keys.append(request.headers["authorization"])
        headers = {"x-request-id": next(self.ids)}
        if self.refuse_every and self.calls % self.refuse_every == 0:
            return httpx.Response(429, headers=headers, json={"error": {"message": "slow down"}})
        out = body["max_tokens"]
        usage = {"prompt_tokens": 90, "completion_tokens": out, "total_tokens": 90 + out}
        if not body.get("stream"):
            return httpx.Response(200, headers=headers, json={"choices": [], "usage": usage})
        chunks = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(out)]
        chunks.append({"choices": [], "usage": usage})
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers=headers, content=sse.encode())


def test_the_mix_is_half_plain_four_in_ten_streamed_one_in_ten_hung_up() -> None:
    kinds = mix(100)

    assert (kinds.count("plain"), kinds.count("stream"), kinds.count("hang-up")) == (50, 40, 10)
    assert mix(4) == ["plain", "stream", "plain", "stream"]


@pytest.mark.asyncio
async def test_each_request_is_recorded_with_the_gateways_id_and_the_servers_usage() -> None:
    gateway = FakeGateway()

    records = await run_metering(
        "http://gateway", "m", {"support": "key-s", "billing": "key-b"}, requests=10,
        concurrency=2, max_tokens=8, hang_up_after=3, transport=httpx.MockTransport(gateway.handle),
    )  # fmt: skip

    assert len(records) == 20
    assert len({r.request_id for r in records}) == 20
    assert gateway.keys.count("Bearer key-s") == gateway.keys.count("Bearer key-b") == 10
    by_kind = {k: [r for r in records if r.kind == k] for k in ("plain", "stream", "hang-up")}
    assert all(r == MeteredRequest(r.request_id, r.tenant, "plain", "completed", 200, 90, 8, 0,
                                   None) for r in by_kind["plain"])  # fmt: skip
    assert all((r.outcome, r.input_tokens, r.output_tokens, r.chunks) == ("completed", 90, 8, 8)
               for r in by_kind["stream"])  # fmt: skip
    assert all((r.outcome, r.input_tokens, r.output_tokens, r.chunks) == ("hung_up", None, None, 3)
               for r in by_kind["hang-up"])  # fmt: skip


@pytest.mark.asyncio
async def test_every_prompt_is_different_so_no_answer_comes_from_a_cache() -> None:
    gateway = FakeGateway()

    await run_metering("http://gateway", "m", {"support": "k"}, requests=5, concurrency=1,
                       transport=httpx.MockTransport(gateway.handle))  # fmt: skip

    prompts = [b["messages"][0]["content"] for b in gateway.bodies]
    assert len(set(prompts)) == 5
    assert all(b["ignore_eos"] and b["temperature"] == 0 for b in gateway.bodies)


@pytest.mark.asyncio
async def test_a_refused_request_is_an_error_with_its_request_id() -> None:
    gateway = FakeGateway(refuse_every=2)

    records = await run_metering(
        "http://gateway",
        "m",
        {"support": "k"},
        requests=4,
        concurrency=1,
        transport=httpx.MockTransport(gateway.handle),
    )

    refused = [r for r in records if r.outcome == "error"]
    assert len(refused) == 2
    assert all(r.status_code == 429 and r.request_id and "slow down" in r.error for r in refused)


@pytest.mark.asyncio
async def test_a_connection_failure_is_an_error_not_a_crash() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    records = await run_metering("http://gateway", "m", {"support": "k"}, requests=2,
                                 concurrency=1, transport=httpx.MockTransport(handle))  # fmt: skip

    assert [(r.outcome, r.request_id, r.status_code) for r in records] == [
        ("error", None, None)
    ] * 2


def test_the_summary_counts_outcomes_per_tenant() -> None:
    records = [
        MeteredRequest("a", "support", "plain", "completed", 200, 90, 8, 0, None),
        MeteredRequest("b", "support", "hang-up", "hung_up", 200, None, None, 3, None),
        MeteredRequest("c", "billing", "plain", "error", 429, None, None, 0, "HTTP 429"),
    ]

    assert summary(records) == [
        "billing: 1 requests: 0 completed, 0 hung up, 1 errors",
        "support: 2 requests: 1 completed, 1 hung up, 0 errors",
    ]
