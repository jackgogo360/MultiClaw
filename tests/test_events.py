import asyncio
import pytest
from multiclaw.events.types import Event, AgentStateEvent, AgentState
from multiclaw.events.bus import EventBus


class TestEvent:
    def test_event_serializes_to_dict(self):
        event = AgentStateEvent(
            agent_id="agent-1",
            from_state=AgentState.IDLE,
            to_state=AgentState.THINKING,
        )
        d = event.model_dump()

        assert d["type"] == "agent.state_change"
        assert d["agent_id"] == "agent-1"
        assert d["from_state"] == "IDLE"
        assert d["to_state"] == "THINKING"
        assert "timestamp" in d

    def test_event_timestamp_is_utc(self):
        event = Event(type="test.event", data={})
        assert event.timestamp.tzinfo is not None


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test.event", handler)
        event = Event(type="test.event", data={"msg": "hello"})
        await bus.publish(event)

        assert len(received) == 1
        assert received[0].type == "test.event"
        assert received[0].data == {"msg": "hello"}

    @pytest.mark.asyncio
    async def test_multiple_handlers(self):
        bus = EventBus()
        results = []

        bus.subscribe("test.event", lambda e: results.append("a"))
        bus.subscribe("test.event", lambda e: results.append("b"))
        await bus.publish(Event(type="test.event", data={}))

        assert results == ["a", "b"]

    @pytest.mark.asyncio
    async def test_wildcard_handler(self):
        bus = EventBus()
        received = []

        bus.subscribe("*", lambda e: received.append(e.type))
        await bus.publish(Event(type="foo.bar", data={}))
        await bus.publish(Event(type="baz.qux", data={}))

        assert received == ["foo.bar", "baz.qux"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        sub_id = bus.subscribe("test.event", handler)
        bus.unsubscribe(sub_id)
        await bus.publish(Event(type="test.event", data={}))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_bus(self):
        bus = EventBus()
        second_called = False

        async def failing_handler(event):
            raise RuntimeError("boom")

        async def good_handler(event):
            nonlocal second_called
            second_called = True

        bus.subscribe("test.event", failing_handler)
        bus.subscribe("test.event", good_handler)
        await bus.publish(Event(type="test.event", data={}))

        assert second_called is True

    @pytest.mark.asyncio
    async def test_publish_awaits_all_handlers(self):
        bus = EventBus()
        slow_done = False

        async def slow_handler(event):
            nonlocal slow_done
            await asyncio.sleep(0.1)
            slow_done = True

        bus.subscribe("test.event", slow_handler)
        await bus.publish(Event(type="test.event", data={}))

        assert slow_done is True
