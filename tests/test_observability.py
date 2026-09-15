from multiclaw.observability import (
    InvalidMetricLabelError,
    OperationalMetrics,
    TraceEventSink,
    observability_scope,
    record_plan_operation,
)


def test_plan_metric_family_rejects_identifier_labels() -> None:
    metrics = OperationalMetrics()
    metrics.increment(
        "multiclaw_plan_operations_total",
        labels={"operation": "step", "status": "succeeded", "error_class": "none"},
    )
    for key in ("plan_id", "run_id", "tenant_id", "session_id", "provider_name", "path"):
        try:
            metrics.increment("multiclaw_plan_operations_total", labels={key: "id"})
        except InvalidMetricLabelError:
            continue
        raise AssertionError(f"identifier label accepted: {key}")


async def test_plan_operation_trace_is_redacted() -> None:
    metrics = OperationalMetrics()
    trace = TraceEventSink()
    async with observability_scope(metrics=metrics, trace_sink=trace):
        record_plan_operation(
            "materialization",
            attributes={"plan_id": "plan-secret", "error": "Authorization: Bearer canary"},
        )
    assert trace.events == [
        (
            "plan_materialization",
            {"plan_id": "plan-secret", "error": "Authorization=[REDACTED]", "status": "succeeded"},
        )
    ]
