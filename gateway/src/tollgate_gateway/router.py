"""The routing rule: which backend serves a request, and whether it may leave the network.

ADR 0013. .NET: choose_route() is a pure static method. It reads a snapshot of the backends' load
and returns a decision; it does no I/O, so every rule is tested with one call and no fakes
(test_router.py).

The rule, in order:
1. The client asked for the hosted model: allowed only if the tenant's data policy says so.
2. Otherwise local: a backend with a free slot, the preferred kind first (vLLM before the nodes),
   and among equals the one with the most free slots.
3. No free local slot: a hosted_allowed tenant overflows to the hosted model; a local_only tenant
   waits in a local queue, the preferred kind first, then the shortest.
4. No local backend up: hosted_allowed goes hosted; local_only gets a 503.
A local_only request never gets a "hosted" decision. hosted.py checks the policy again anyway.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BackendLoad:
    """One local backend at one moment, as the load tracker last saw it (backends.py)."""

    name: str
    up: bool
    busy: int  # requests running or waiting there
    slots: int  # requests it serves at once
    priority: int  # lower is preferred

    @property
    def free(self) -> int:
        """Free slots; negative when requests are waiting."""
        return self.slots - self.busy


@dataclass(frozen=True)
class Route:
    kind: Literal["local", "hosted", "refused"]
    reason: str  # free_slot | queued | overflow | no_local_backend | requested, or an error code
    backends: tuple[
        str, ...
    ] = ()  # local only: the order to try them in (the first, then failover)
    status: int = 200  # refused only: HTTP status, error code and message for the client
    code: str = ""
    message: str = ""


def refuse(status: int, code: str, message: str) -> Route:
    return Route("refused", code, status=status, code=code, message=message)


def _preference(load: BackendLoad) -> tuple:
    # Free before full; then the preferred kind; then the most room (or the shortest queue).
    return (load.free <= 0, load.priority, -load.free, load.name)


def local_order(loads: list[BackendLoad]) -> tuple[str, ...]:
    """The backends that are up, best first: the order to try them in."""
    return tuple(load.name for load in sorted((x for x in loads if x.up), key=_preference))


def choose_route(
    loads: list[BackendLoad],
    *,
    wants_hosted: bool,
    allows_hosted: bool,
    hosted_configured: bool,
    hosted_unsupported: str | None,
) -> Route:
    """Decide where one request goes.

    wants_hosted: the client named the hosted model. allows_hosted: the tenant's data policy.
    hosted_configured: an Anthropic API key is set. hosted_unsupported: why the hosted model
    can't take this request (tools, images...), or None if it can.
    """
    if wants_hosted:
        if not allows_hosted:
            return refuse(
                403,
                "data_policy",
                "Your tenant's data policy keeps requests on local models; the hosted model is "
                "not available to it.",
            )
        if not hosted_configured:
            return refuse(404, "model_not_found", "The hosted model is not configured.")
        if hosted_unsupported:
            return refuse(400, "unsupported_by_hosted_model", hosted_unsupported)
        return Route("hosted", "requested")

    can_overflow = allows_hosted and hosted_configured and hosted_unsupported is None
    order = local_order(loads)
    if not order:
        if can_overflow:
            return Route("hosted", "no_local_backend")
        return refuse(503, "no_backend_available", "No local model server is available.")
    first = next(load for load in loads if load.name == order[0])
    if first.free > 0:
        return Route("local", "free_slot", order)
    if can_overflow:
        return Route("hosted", "overflow")
    return Route("local", "queued", order)
