from dataclasses import FrozenInstanceError

import pytest

from multiclaw.tenancy.context import TenantContext


def test_tenant_context_derives_root_session_and_run_without_mutation() -> None:
    root = TenantContext(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        request_started_at_ms=123,
    )

    session = root.for_session("session-1")
    run = session.for_run("session-1", "run-1")

    assert root.tenant_id == "tenant-1"
    assert root.workspace_id == "workspace-1"
    assert root.session_id is None
    assert root.run_id is None
    assert root.request_started_at_ms == 123

    assert session.tenant_id == root.tenant_id
    assert session.workspace_id == root.workspace_id
    assert session.session_id == "session-1"
    assert session.run_id is None
    assert session.request_started_at_ms == 123

    assert run.tenant_id == root.tenant_id
    assert run.workspace_id == root.workspace_id
    assert run.session_id == "session-1"
    assert run.run_id == "run-1"
    assert run.request_started_at_ms == 123

    with pytest.raises(FrozenInstanceError):
        run.tenant_id = "tenant-2"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tenant_id": "", "workspace_id": "workspace-1"}, "tenant_id"),
        ({"tenant_id": "tenant-1", "workspace_id": ""}, "workspace_id"),
        (
            {
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
                "run_id": "run-1",
            },
            "session_id",
        ),
    ],
)
def test_tenant_context_rejects_invalid_root_values(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TenantContext(**kwargs)


def test_tenant_context_requires_non_empty_session_and_run_ids() -> None:
    root = TenantContext(tenant_id="tenant-1", workspace_id="workspace-1")
    run = root.for_run("session-1", "run-1")

    with pytest.raises(ValueError, match="session_id"):
        root.for_session("")

    with pytest.raises(ValueError, match="session_id"):
        root.for_run("", "run-1")

    with pytest.raises(ValueError, match="run_id"):
        root.for_run("session-1", "")

    replacement = run.for_session("session-2")
    assert replacement.session_id == "session-2"
    assert replacement.run_id is None


def test_tenant_context_does_not_expose_scope_swapping_helpers() -> None:
    assert not hasattr(TenantContext, "for_tenant")
    assert not hasattr(TenantContext, "for_workspace")
    assert not hasattr(TenantContext, "with_tenant")
    assert not hasattr(TenantContext, "with_workspace")
