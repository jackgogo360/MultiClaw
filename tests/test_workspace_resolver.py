from pathlib import Path

import pytest

from multiclaw.tenancy import TenantContext
from multiclaw.tenancy.workspace import (
    InvalidWorkspaceScope,
    WorkspaceContainmentError,
    WorkspaceResolver,
)
from multiclaw.tools.read_file import ReadFileToolBuilder


def test_workspace_resolver_maps_each_scope_to_distinct_directory(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path)

    a = resolver.resolve(TenantContext("tenant-a", "workspace-a"))
    b = resolver.resolve(TenantContext("tenant-b", "workspace-b"))

    assert a == (tmp_path / "tenant-a" / "workspace-a").resolve()
    assert b == (tmp_path / "tenant-b" / "workspace-b").resolve()
    assert a != b


def test_workspace_resolver_recomputes_the_same_path_for_the_same_scope(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path)
    context = TenantContext("tenant-a", "workspace-a")

    first = resolver.resolve(context)
    second = resolver.resolve(context)

    assert first == second


@pytest.mark.parametrize(
    ("tenant_id", "workspace_id"),
    [
        ("../escape", "workspace"),
        ("/absolute", "workspace"),
        ("x\x00y", "workspace"),
        ("a/b", "workspace"),
        ("a\\b", "workspace"),
        ("_tenant", "workspace"),
        (".", "workspace"),
        ("..", "workspace"),
        ("a" * 65, "workspace"),
        ("tenant", "../escape"),
        ("tenant", "/absolute"),
        ("tenant", "x\x00y"),
        ("tenant", "a/b"),
        ("tenant", "a\\b"),
        ("tenant", "_workspace"),
        ("tenant", "."),
        ("tenant", ".."),
        ("tenant", "b" * 65),
    ],
)
def test_workspace_resolver_rejects_non_identifier_segments(
    tmp_path: Path,
    tenant_id: str,
    workspace_id: str,
) -> None:
    resolver = WorkspaceResolver(tmp_path)

    with pytest.raises(InvalidWorkspaceScope):
        resolver.resolve(TenantContext(tenant_id, workspace_id))


def test_workspace_resolver_accepts_max_length_segments(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path)
    tenant_id = "t" * 64
    workspace_id = "w" * 64

    resolved = resolver.resolve(TenantContext(tenant_id, workspace_id))

    assert resolved == (tmp_path / tenant_id / workspace_id).resolve()


def test_resolver_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "tenant-a").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceContainmentError):
        WorkspaceResolver(root).resolve(TenantContext("tenant-a", "workspace-a"))


def test_workspace_resolver_creates_missing_directories_with_owner_only_permissions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    resolved = WorkspaceResolver(root).resolve(
        TenantContext("tenant-a", "workspace-a"),
        create=True,
    )

    assert resolved == (root / "tenant-a" / "workspace-a").resolve()
    assert resolved.is_dir()
    assert resolved.parent.is_dir()
    assert resolved.stat().st_mode & 0o777 == 0o700
    assert resolved.parent.stat().st_mode & 0o777 == 0o700


def test_workspace_resolver_never_accepts_client_supplied_path_segments(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path)

    with pytest.raises(InvalidWorkspaceScope):
        resolver.resolve(TenantContext("tenant-a", "../../etc"))


@pytest.mark.asyncio
async def test_read_file_builder_uses_resolver_workspace_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    workspace = WorkspaceResolver(root).resolve(
        TenantContext("tenant-a", "workspace-a"),
        create=True,
    )
    source = workspace / "demo.txt"
    source.write_text("hello\n", encoding="utf-8")

    builder = ReadFileToolBuilder(str(workspace))
    result = await builder.build(builder.validate({"file_path": "demo.txt"})).execute()

    assert builder.workspace_root == workspace
    assert result.status == "success"
    assert result.data["path"] == str(source.resolve())
