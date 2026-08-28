import asyncio

from multiclaw.observability import (
    OperationalMetrics,
    TraceEventSink,
    current_metrics,
    current_trace_sink,
    increment_metric,
    record_trace_event,
)


async def _run_scoped(metric_name: str, metrics, trace_sink, scope_cm):
    async with scope_cm(metrics=metrics, trace_sink=trace_sink):
        increment_metric(metric_name, labels={"backend": "sqlite", "operation": "demo", "status": "ok"})
        record_trace_event(metric_name, attributes={"session_id": "secret-session", "path": "/tmp/private"})
        await asyncio.sleep(0)


def test_observability_scope_restores_default_collector_after_exit():
    from multiclaw.observability import observability_scope

    default_metrics = current_metrics()
    default_metrics.clear()
    default_trace = current_trace_sink()
    default_trace.clear()
    scoped_metrics = OperationalMetrics()
    scoped_trace = TraceEventSink()

    async def run():
        async with observability_scope(metrics=scoped_metrics, trace_sink=scoped_trace):
            assert current_metrics() is scoped_metrics
            assert current_trace_sink() is scoped_trace
            increment_metric("inside", labels={"backend": "sqlite", "operation": "demo", "status": "ok"})

    asyncio.run(run())

    assert current_metrics() is default_metrics
    assert current_trace_sink() is default_trace
    assert any(metric_name == "inside" for metric_name, _labels in scoped_metrics.counters)
    assert default_metrics.counters == {}


def test_observability_scope_isolates_concurrent_scopes_and_child_tasks():
    from multiclaw.observability import observability_scope

    metrics_a = OperationalMetrics()
    metrics_b = OperationalMetrics()
    trace_a = TraceEventSink()
    trace_b = TraceEventSink()

    async def child(metric_name: str):
        increment_metric(metric_name, labels={"backend": "sqlite", "operation": "child", "status": "ok"})
        record_trace_event(metric_name, attributes={"tenant_id": "tenant-a", "path": "C:\\secret.txt"})

    async def runner(metric_name: str, metrics, trace_sink):
        async with observability_scope(metrics=metrics, trace_sink=trace_sink):
            increment_metric(metric_name, labels={"backend": "sqlite", "operation": "parent", "status": "ok"})
            await asyncio.create_task(child(metric_name))

    async def main():
        await asyncio.gather(
            runner("metric_a", metrics_a, trace_a),
            runner("metric_b", metrics_b, trace_b),
        )

    asyncio.run(main())

    assert any(metric_name == "metric_a" for metric_name, _labels in metrics_a.counters)
    assert not any(metric_name == "metric_b" for metric_name, _labels in metrics_a.counters)
    assert any(metric_name == "metric_b" for metric_name, _labels in metrics_b.counters)
    assert not any(metric_name == "metric_a" for metric_name, _labels in metrics_b.counters)
    assert trace_a.events and trace_b.events
    assert "tenant-a" not in str(trace_a.events)
    assert "C:\\secret.txt" not in str(trace_b.events)
