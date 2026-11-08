"""Backends and their load: reading llama-server and vLLM /metrics, and the tracker polling them."""

import httpx
import pytest
import pytest_asyncio
from tollgate_gateway.backends import Backend, LoadTracker, parse_load

pytestmark = pytest.mark.asyncio

LLAMA_METRICS = """\
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 2
# HELP llamacpp:requests_deferred Number of requests deferred.
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 3
llamacpp:prompt_tokens_total 1234
"""

VLLM_METRICS = """\
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="smollm3-3b"} 7.0
vllm:num_requests_waiting{engine="0",model_name="smollm3-3b"} 1.0
vllm:num_requests_waiting_by_reason{engine="0",reason="capacity"} 1.0
"""

BACKENDS = [
    Backend(name="vllm", url="http://vllm:8000", kind="vllm", slots=32, priority=0),
    Backend(name="node-1", url="http://node-1:8080", kind="llama", slots=2, priority=1),
]


# --- reading /metrics ----------------------------------------------------------------------------


async def test_llama_server_load_is_processing_plus_deferred() -> None:
    assert parse_load("llama", LLAMA_METRICS) == 5


async def test_vllm_load_is_running_plus_waiting() -> None:
    # Exact names only: vllm:num_requests_waiting_by_reason must not be counted as well.
    assert parse_load("vllm", VLLM_METRICS) == 8


async def test_missing_metrics_count_as_idle() -> None:
    assert parse_load("llama", "") == 0


# --- the tracker ---------------------------------------------------------------------------------


class FakeServers:
    """Each host answers /metrics with its text, an HTTP status, or refuses the connection."""

    def __init__(self) -> None:
        self.answers: dict[str, str | int] = {"vllm": VLLM_METRICS, "node-1": LLAMA_METRICS}
        self.paths: list[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        answer = self.answers.get(request.url.host)
        if answer is None:
            raise httpx.ConnectError("connection refused")
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, text=answer)


@pytest.fixture
def servers() -> FakeServers:
    return FakeServers()


@pytest_asyncio.fixture
async def tracker(servers: FakeServers):
    async with httpx.AsyncClient(transport=httpx.MockTransport(servers.handle)) as http:
        yield LoadTracker(BACKENDS, http)


def by_name(tracker: LoadTracker) -> dict:
    return {load.name: load for load in tracker.snapshot()}


async def test_before_the_first_poll_every_backend_counts_as_up_and_idle(tracker) -> None:
    loads = by_name(tracker)

    assert loads["vllm"].up and loads["vllm"].busy == 0
    assert loads["node-1"].up and loads["node-1"].busy == 0


async def test_the_snapshot_carries_slots_and_priority(tracker) -> None:
    vllm = by_name(tracker)["vllm"]

    assert (vllm.slots, vllm.priority) == (32, 0)


async def test_a_poll_reads_every_backends_load(tracker, servers) -> None:
    await tracker.poll_once()

    loads = by_name(tracker)
    assert loads["vllm"].busy == 8
    assert loads["node-1"].busy == 5
    assert sorted(servers.paths) == ["/metrics", "/metrics"]


async def test_an_unreachable_backend_is_down(tracker, servers) -> None:
    del servers.answers["vllm"]  # the workstation is switched off

    await tracker.poll_once()

    assert not by_name(tracker)["vllm"].up
    assert by_name(tracker)["node-1"].up


async def test_a_backend_answering_with_an_error_is_down(tracker, servers) -> None:
    servers.answers["node-1"] = 503  # llama-server answers 503 while it loads the model

    await tracker.poll_once()

    assert not by_name(tracker)["node-1"].up


async def test_a_backend_is_back_on_the_next_good_poll(tracker, servers) -> None:
    servers.answers["node-1"] = 503
    await tracker.poll_once()
    servers.answers["node-1"] = LLAMA_METRICS

    await tracker.poll_once()

    assert by_name(tracker)["node-1"].up


async def test_requests_sent_since_the_last_poll_count_as_busy(tracker) -> None:
    # Without this, every request in the second after a poll would pick the same "idle" backend.
    await tracker.poll_once()
    tracker.sent("node-1")
    tracker.sent("node-1")

    assert by_name(tracker)["node-1"].busy == 5 + 2

    await tracker.poll_once()  # the server's own count now includes them

    assert by_name(tracker)["node-1"].busy == 5


async def test_mark_down_takes_a_backend_out_until_the_next_poll(tracker) -> None:
    tracker.mark_down("vllm")
    assert not by_name(tracker)["vllm"].up

    await tracker.poll_once()

    assert by_name(tracker)["vllm"].up


async def test_backends_are_looked_up_by_name(tracker) -> None:
    assert tracker.backend("node-1").url == "http://node-1:8080"
