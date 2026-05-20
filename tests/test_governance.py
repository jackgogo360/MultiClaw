import asyncio

import pytest
from pydantic import ValidationError


@pytest.mark.asyncio
async def test_permission_checker_allows_safe_tool():
    from multiclaw.governance import PermissionChecker

    checker = PermissionChecker(guarded_tools={"delete_file"})

    decision = await checker.check("echo")

    assert decision.allow is True
    assert decision.requires_approval is False
    assert decision.reason == "allowed"


@pytest.mark.asyncio
async def test_permission_checker_requires_approval_for_guarded_tool():
    from multiclaw.governance import PermissionChecker

    checker = PermissionChecker(guarded_tools={"delete_file"})

    decision = await checker.check("delete_file")

    assert decision.allow is True
    assert decision.requires_approval is True
    assert decision.reason == "approval_required"


@pytest.mark.asyncio
async def test_permission_checker_canonicalizes_guarded_tool_names():
    from multiclaw.governance import PermissionChecker

    checker = PermissionChecker(guarded_tools={"  Delete_File  "})

    decision = await checker.check(" delete_file ")

    assert decision.allow is True
    assert decision.requires_approval is True
    assert decision.reason == "approval_required"


@pytest.mark.asyncio
async def test_permission_checker_requires_approval_for_external_workspace_path(tmp_path):
    from multiclaw.governance import PermissionChecker

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello", encoding="utf-8")

    checker = PermissionChecker()

    decision = await checker.check(
        "read_file",
        {"file_path": str(outside)},
        workspace_root=workspace,
    )

    assert decision.allow is True
    assert decision.requires_approval is True
    assert decision.reason == "external_path_requires_approval"
    assert decision.approved_roots == [str(outside.resolve())]


@pytest.mark.asyncio
async def test_in_memory_audit_logger_records_and_lists_entries():
    from multiclaw.governance import AuditLog, InMemoryAuditLogger

    logger = InMemoryAuditLogger()

    entry = await logger.record(tool_name="echo", status="success", detail="completed")
    entries = await logger.list_entries()

    assert isinstance(entry, AuditLog)
    assert entry.tool_name == "echo"
    assert entry.status == "success"
    assert entry.detail == "completed"
    assert entry.timestamp.tzinfo is not None
    assert entry.timestamp.utcoffset() is not None
    assert entry.timestamp.utcoffset().total_seconds() == 0
    assert len(entries) == 1
    assert entries[0].tool_name == "echo"
    assert entries[0].status == "success"
    assert entries[0].detail == "completed"


@pytest.mark.asyncio
async def test_in_memory_audit_logger_history_is_tamper_resistant():
    from multiclaw.governance import InMemoryAuditLogger

    logger = InMemoryAuditLogger()
    await logger.record(tool_name="echo", status="success", detail="completed")

    entries = await logger.list_entries()
    with pytest.raises(ValidationError):
        entries[0].detail = "tampered"

    fresh_entries = await logger.list_entries()

    assert fresh_entries[0].detail == "completed"


@pytest.mark.asyncio
async def test_process_sandbox_runs_async_callable():
    from multiclaw.governance import ProcessSandbox

    sandbox = ProcessSandbox()

    async def operation():
        return "done"

    result = await sandbox.run(operation)

    assert result == "done"


@pytest.mark.asyncio
async def test_process_sandbox_runs_sync_callable():
    from multiclaw.governance import ProcessSandbox

    sandbox = ProcessSandbox()

    def operation():
        return "done"

    result = await sandbox.run(operation)

    assert result == "done"


@pytest.mark.asyncio
async def test_process_sandbox_times_out_async_operation():
    from multiclaw.governance import ProcessSandbox, SandboxTimeoutError

    sandbox = ProcessSandbox(timeout=0.05)

    async def operation():
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(SandboxTimeoutError, match="timed out"):
        await sandbox.run(operation)


@pytest.mark.asyncio
async def test_process_sandbox_times_out_sync_operation():
    from multiclaw.governance import ProcessSandbox, SandboxTimeoutError

    sandbox = ProcessSandbox(timeout=0.05)

    def operation():
        import time
        time.sleep(10)
        return "never"

    with pytest.raises(SandboxTimeoutError, match="timed out"):
        await sandbox.run(operation)


def test_governance_package_exports():
    from multiclaw import governance
    from multiclaw.governance import (
        AuditLog,
        InMemoryAuditLogger,
        PermissionChecker,
        PermissionDecision,
        ProcessSandbox,
    )

    assert governance.AuditLog is AuditLog
    assert governance.InMemoryAuditLogger is InMemoryAuditLogger
    assert governance.PermissionChecker is PermissionChecker
    assert governance.PermissionDecision is PermissionDecision
    assert governance.ProcessSandbox is ProcessSandbox
