"""The routing rule: which backend serves a request, and when it may leave the network (ADR 0013).

choose_route() is a pure function of the request, the tenant's data policy and the backends' load,
so every case is one call with no fakes.
"""

from tollgate_gateway.router import BackendLoad, Route, choose_route
from tollgate_gateway.tenants import Tenant


def load(name: str, busy: int = 0, slots: int = 2, priority: int = 1, up: bool = True):
    return BackendLoad(name=name, up=up, busy=busy, slots=slots, priority=priority)


def vllm(busy: int = 0, up: bool = True) -> BackendLoad:
    return load("vllm", busy=busy, slots=32, priority=0, up=up)


def route(
    loads: list[BackendLoad],
    wants_hosted: bool = False,
    allows_hosted: bool = False,
    hosted_configured: bool = True,
    unsupported: str | None = None,
) -> Route:
    return choose_route(
        loads,
        wants_hosted=wants_hosted,
        allows_hosted=allows_hosted,
        hosted_configured=hosted_configured,
        hosted_unsupported=unsupported,
    )


# --- local backends ------------------------------------------------------------------------------


def test_the_preferred_backend_wins_while_it_has_a_free_slot() -> None:
    chosen = route([load("node-1"), vllm(busy=20), load("node-2")])

    assert (chosen.kind, chosen.reason) == ("local", "free_slot")
    assert chosen.backends[0] == "vllm"


def test_a_full_preferred_backend_gives_way_to_the_node_with_most_room() -> None:
    chosen = route([vllm(busy=32), load("node-1", busy=1), load("node-2", busy=0)])

    assert chosen.backends[0] == "node-2"


def test_the_failover_order_lists_free_backends_first() -> None:
    chosen = route([vllm(busy=32), load("node-1", busy=1), load("node-2", busy=0)])

    assert chosen.backends == ("node-2", "node-1", "vllm")


def test_a_down_backend_is_never_chosen() -> None:
    chosen = route([vllm(up=False), load("node-1", busy=1)])

    assert chosen.backends == ("node-1",)


def test_when_everything_is_full_local_only_waits_in_the_shortest_queue() -> None:
    chosen = route([vllm(up=False), load("node-1", busy=4), load("node-2", busy=3)])

    assert (chosen.kind, chosen.reason) == ("local", "queued")
    assert chosen.backends[0] == "node-2"  # 1 waiting, against 2 on node-1


def test_no_local_backend_up_is_503_for_local_only() -> None:
    chosen = route([vllm(up=False), load("node-1", up=False)])

    assert chosen.kind == "refused"
    assert (chosen.status, chosen.code) == (503, "no_backend_available")


# --- overflow to the hosted model ----------------------------------------------------------------

FULL = [vllm(up=False), load("node-1", busy=2), load("node-2", busy=3)]


def test_hosted_allowed_overflows_when_everything_is_full() -> None:
    chosen = route(FULL, allows_hosted=True)

    assert (chosen.kind, chosen.reason) == ("hosted", "overflow")


def test_hosted_allowed_stays_local_while_there_is_room() -> None:
    chosen = route([load("node-1", busy=1)], allows_hosted=True)

    assert chosen.kind == "local"


def test_hosted_allowed_goes_hosted_when_no_local_backend_is_up() -> None:
    chosen = route([load("node-1", up=False)], allows_hosted=True)

    assert (chosen.kind, chosen.reason) == ("hosted", "no_local_backend")


def test_no_overflow_when_the_hosted_model_is_not_configured() -> None:
    chosen = route(FULL, allows_hosted=True, hosted_configured=False)

    assert (chosen.kind, chosen.reason) == ("local", "queued")


def test_no_overflow_for_a_request_the_hosted_model_cannot_take() -> None:
    chosen = route(FULL, allows_hosted=True, unsupported="tools are not supported")

    assert (chosen.kind, chosen.reason) == ("local", "queued")


# --- asking for the hosted model -----------------------------------------------------------------


def test_asking_for_the_hosted_model_goes_hosted_even_when_local_is_free() -> None:
    chosen = route([vllm()], wants_hosted=True, allows_hosted=True)

    assert (chosen.kind, chosen.reason) == ("hosted", "requested")


def test_local_only_asking_for_the_hosted_model_is_403() -> None:
    chosen = route([vllm()], wants_hosted=True, allows_hosted=False)

    assert chosen.kind == "refused"
    assert (chosen.status, chosen.code) == (403, "data_policy")


def test_asking_for_a_hosted_model_that_is_not_configured_is_404() -> None:
    chosen = route([vllm()], wants_hosted=True, allows_hosted=True, hosted_configured=False)

    assert (chosen.status, chosen.code) == (404, "model_not_found")


def test_asking_the_hosted_model_for_something_it_cannot_take_is_400() -> None:
    chosen = route([vllm()], wants_hosted=True, allows_hosted=True, unsupported="no images")

    assert (chosen.status, chosen.code) == (400, "unsupported_by_hosted_model")
    assert chosen.message == "no images"


# --- the tenant's data policy --------------------------------------------------------------------


def test_only_hosted_allowed_lets_a_request_leave_the_network() -> None:
    assert Tenant(id="t", name="T", data_policy="hosted_allowed").allows_hosted
    assert not Tenant(id="t", name="T", data_policy="local_only").allows_hosted
    assert not Tenant(id="t", name="T").allows_hosted  # the default


def test_an_unknown_data_policy_keeps_requests_local() -> None:
    # A typo in the database must fail closed.
    assert not Tenant(id="t", name="T", data_policy="Hosted_Allowed").allows_hosted


def test_when_everything_is_full_the_preferred_backend_still_comes_first() -> None:
    # vLLM's queue moves about 50 times faster than a node's (bench/RESULTS.md).
    chosen = route([load("node-1", busy=2), vllm(busy=32)])

    assert (chosen.kind, chosen.reason) == ("local", "queued")
    assert chosen.backends == ("vllm", "node-1")


def test_local_order_is_the_up_backends_best_first() -> None:
    from tollgate_gateway.router import local_order

    loads = [load("node-1", busy=1), vllm(busy=32), load("node-2"), load("node-3", up=False)]

    assert local_order(loads) == ("node-2", "node-1", "vllm")
