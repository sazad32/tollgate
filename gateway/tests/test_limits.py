"""The token bucket against an in-memory Redis that runs the real Lua scripts (fakeredis + lupa)."""

import asyncio

import fakeredis
import pytest
import pytest_asyncio
from tollgate_gateway.limits import (
    DEFAULT_MAX_TOKENS,
    TEMPLATE_ALLOWANCE,
    Limit,
    TokenBuckets,
    estimate_tokens,
    prompt_estimate,
)

pytestmark = pytest.mark.asyncio

LIMIT = Limit(tokens_per_minute=600)  # capacity 600, refills 10 tokens per second


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest_asyncio.fixture
async def buckets(clock):
    redis = fakeredis.FakeAsyncRedis()
    yield TokenBuckets(redis, clock=clock)
    await redis.aclose()


def body(text: str = "x" * 300, **extra: object) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": text}], **extra}


# --- estimates -----------------------------------------------------------------------------------


async def test_prompt_estimate_is_characters_over_three_plus_the_template() -> None:
    assert prompt_estimate(body("x" * 300)) == 100 + TEMPLATE_ALLOWANCE


async def test_estimate_adds_max_tokens() -> None:
    assert estimate_tokens(body(max_tokens=128)) == 100 + TEMPLATE_ALLOWANCE + 128


async def test_estimate_assumes_a_default_when_max_tokens_is_missing() -> None:
    assert estimate_tokens(body()) == 100 + TEMPLATE_ALLOWANCE + DEFAULT_MAX_TOKENS


async def test_estimate_accepts_the_newer_max_completion_tokens_field() -> None:
    assert estimate_tokens(body(max_completion_tokens=64)) == 100 + TEMPLATE_ALLOWANCE + 64


async def test_estimate_counts_every_message_and_content_part() -> None:
    request = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "a" * 30},
            {"role": "user", "content": [{"type": "text", "text": "b" * 60}]},
        ],
        "max_tokens": 10,
    }
    assert estimate_tokens(request) == 30 + TEMPLATE_ALLOWANCE + 10


# --- the bucket ----------------------------------------------------------------------------------


async def test_a_full_bucket_allows_and_takes_the_estimate(buckets) -> None:
    decision = await buckets.reserve("support", LIMIT, 200)

    assert decision.allowed
    assert decision.remaining == pytest.approx(400)
    assert decision.retry_after_s == 0


async def test_not_enough_tokens_is_refused_and_nothing_is_taken(buckets) -> None:
    await buckets.reserve("support", LIMIT, 500)

    decision = await buckets.reserve("support", LIMIT, 300)

    assert not decision.allowed
    assert decision.retry_after_s == pytest.approx(20)  # 200 short, at 10 tokens per second
    assert await buckets.level("support", LIMIT) == pytest.approx(100)


async def test_the_bucket_refills_over_time(buckets, clock) -> None:
    await buckets.reserve("support", LIMIT, 600)
    clock.now += 30

    assert await buckets.level("support", LIMIT) == pytest.approx(300)


async def test_it_never_refills_past_capacity(buckets, clock) -> None:
    await buckets.reserve("support", LIMIT, 100)
    clock.now += 3600

    assert await buckets.level("support", LIMIT) == pytest.approx(600)


async def test_settling_refunds_what_was_not_used(buckets) -> None:
    await buckets.reserve("support", LIMIT, 500)

    level = await buckets.settle("support", LIMIT, reserved=500, actual=120)

    assert level == pytest.approx(480)


async def test_settling_more_than_reserved_leaves_a_debt(buckets, clock) -> None:
    await buckets.reserve("support", LIMIT, 500)
    level = await buckets.settle("support", LIMIT, reserved=500, actual=900)

    assert level == pytest.approx(-300)
    assert not (await buckets.reserve("support", LIMIT, 50)).allowed
    clock.now += 35  # 350 tokens refilled: the debt is paid and 50 are back
    assert (await buckets.reserve("support", LIMIT, 50)).allowed


async def test_a_refund_never_overfills_the_bucket(buckets) -> None:
    level = await buckets.settle("support", LIMIT, reserved=500, actual=0)  # never reserved

    assert level == pytest.approx(600)


async def test_a_request_bigger_than_the_bucket_waits_for_a_full_bucket(buckets, clock) -> None:
    await buckets.reserve("support", LIMIT, 100)
    refused = await buckets.reserve("support", LIMIT, 1000)
    assert not refused.allowed
    assert refused.retry_after_s == pytest.approx(10)  # until full, not until 1000 (never)

    clock.now += 10
    allowed = await buckets.reserve("support", LIMIT, 1000)
    assert allowed.allowed
    assert allowed.remaining == pytest.approx(-400)  # admitted, and in debt for the excess


async def test_concurrent_reservations_never_overspend(buckets) -> None:
    decisions = await asyncio.gather(*(buckets.reserve("support", LIMIT, 100) for _ in range(50)))

    assert sum(d.allowed for d in decisions) == 6
    assert await buckets.level("support", LIMIT) == pytest.approx(0)


async def test_tenants_have_separate_buckets(buckets) -> None:
    await buckets.reserve("support", LIMIT, 600)

    assert (await buckets.reserve("billing", LIMIT, 600)).allowed


async def test_a_clock_going_backwards_adds_no_tokens(buckets, clock) -> None:
    # Two gateway replicas on two nodes: their clocks can disagree by a few milliseconds.
    await buckets.reserve("support", LIMIT, 600)
    clock.now -= 5
    await buckets.reserve("support", LIMIT, 0)
    clock.now += 5

    assert await buckets.level("support", LIMIT) == pytest.approx(0)


async def test_idle_buckets_expire_from_redis(buckets) -> None:
    # An expired bucket reads as full, which is exactly what a long-idle bucket would be.
    await buckets.reserve("support", LIMIT, 300)
    ttl = await buckets.redis.ttl(buckets.key_prefix + "support")

    assert 0 < ttl <= 31  # 300 tokens missing, refilled in 30 s
