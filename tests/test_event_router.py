import pytest
from pydantic import ValidationError

from multiclaw.tenancy import TenantContext


@pytest.mark.asyncio
async def test_event_router_delivers_only_exact_run_scope():
    from multiclaw.events import EventRouter, EventScope, ScopedEvent

    router = EventRouter()
    target = EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="r")
    seen: list[ScopedEvent] = []

    async def collect(event: ScopedEvent) -> None:
        seen.append(event)

    subscription = router.subscribe(target, collect)
    for scope in (
        target,
        EventScope(tenant_id="other", workspace_id="w", session_id="s", run_id="r"),
        EventScope(tenant_id="t", workspace_id="other", session_id="s", run_id="r"),
        EventScope(tenant_id="t", workspace_id="w", session_id="other", run_id="r"),
        EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="other"),
    ):
        await router.publish(ScopedEvent.from_scope(scope, "tool.completed", {}))

    subscription.close()

    assert [(e.tenant_id, e.workspace_id, e.session_id, e.run_id) for e in seen] == [
        ("t", "w", "s", "r")
    ]


@pytest.mark.asyncio
async def test_event_router_close_is_idempotent_and_stops_future_delivery():
    from multiclaw.events import EventRouter, EventScope, ScopedEvent

    router = EventRouter()
    scope = EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="r")
    seen: list[str] = []

    async def collect(event: ScopedEvent) -> None:
        seen.append(event.event_type)

    subscription = router.subscribe(scope, collect)
    subscription.close()
    subscription.close()

    await router.publish(ScopedEvent.from_scope(scope, "tool.completed", {}))

    assert seen == []


@pytest.mark.asyncio
async def test_event_router_handler_failure_does_not_block_other_exact_subscribers():
    from multiclaw.events import EventRouter, EventScope, ScopedEvent

    router = EventRouter()
    scope = EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="r")
    seen: list[str] = []

    async def failing(_event: ScopedEvent) -> None:
        raise RuntimeError("boom")

    async def collect(event: ScopedEvent) -> None:
        seen.append(event.event_type)

    router.subscribe(scope, failing)
    router.subscribe(scope, collect)

    await router.publish(ScopedEvent.from_scope(scope, "tool.completed", {}))

    assert seen == ["tool.completed"]


@pytest.mark.asyncio
async def test_event_router_isolates_payload_between_handlers_and_source_event():
    from multiclaw.events import EventRouter, EventScope, ScopedEvent

    router = EventRouter()
    scope = EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="r")
    source = ScopedEvent.from_scope(
        scope,
        "tool.completed",
        {"outer": {"value": 1}},
    )
    seen: list[dict[str, int]] = []

    async def mutating_handler(event: ScopedEvent) -> None:
        event.data["outer"]["value"] = 99

    async def observing_handler(event: ScopedEvent) -> None:
        seen.append({"value": event.data["outer"]["value"]})

    router.subscribe(scope, mutating_handler)
    router.subscribe(scope, observing_handler)

    await router.publish(source)

    assert seen == [{"value": 1}]
    assert source.data == {"outer": {"value": 1}}


def test_event_scope_requires_non_empty_non_wildcard_fields():
    from multiclaw.events import EventScope

    with pytest.raises(ValidationError):
        EventScope(tenant_id="", workspace_id="w", session_id="s", run_id="r")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="t", workspace_id="", session_id="s", run_id="r")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="t", workspace_id="w", session_id="", run_id="r")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="*", workspace_id="w", session_id="s", run_id="r")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="t", workspace_id="*", session_id="s", run_id="r")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="t", workspace_id="w", session_id="*", run_id="r")

    with pytest.raises(ValidationError):
        EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="*")


def test_event_scope_from_context_requires_session_and_run():
    from multiclaw.events import EventScope

    with pytest.raises(ValueError, match="event scope requires session and run"):
        EventScope.from_context(TenantContext("tenant", "workspace"))

    with pytest.raises(ValueError, match="event scope requires session and run"):
        EventScope.from_context(TenantContext("tenant", "workspace").for_session("session"))
