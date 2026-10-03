from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from multiclaw.collaboration.workspaces import GitWorkspaceManager


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "hello.txt").write_text("before\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


async def test_isolation_diff_and_accept(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    job = str(uuid4())
    workspace = await manager.create(project, job)
    assert await manager.create(project, job) == workspace
    isolated = Path(workspace["workspace_path"])
    (isolated / "hello.txt").write_text("after\n")
    (isolated / "new.txt").write_text("new\n")
    assert (project / "hello.txt").read_text() == "before\n"
    assert not (project / "new.txt").exists()
    diff = await manager.diff(workspace)
    assert set(diff["files"]) == {"hello.txt", "new.txt"}
    assert "+new" in diff["patch"]
    result = await manager.accept(workspace, diff["digest"])
    assert result["accepted"]
    assert (project / "hello.txt").read_text() == "after\n"
    assert (project / "new.txt").read_text() == "new\n"
    assert git(project, "rev-parse", "HEAD") == workspace["base_commit"]


async def test_refuses_dirty_or_changed_source_and_stale_review(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / "hello.txt").write_text("after\n")
    reviewed = await manager.diff(workspace)
    (isolated / "hello.txt").write_text("different\n")
    with pytest.raises(ValueError, match="review|digest"):
        await manager.accept(workspace, reviewed["digest"])
    reviewed = await manager.diff(workspace)
    (project / "new.txt").write_text("local")
    with pytest.raises(ValueError, match="clean|dirty"):
        await manager.accept(workspace, reviewed["digest"])
    (project / "new.txt").unlink()
    git(project, "commit", "--allow-empty", "-m", "changed")
    with pytest.raises(ValueError, match="base|changed"):
        await manager.accept(workspace, reviewed["digest"])
    assert (project / "hello.txt").read_text() == "before\n"
    assert isolated.exists()


async def test_invalid_job_storage_escape_and_dirty_project(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    with pytest.raises(ValueError, match="UUID"):
        await manager.create(project, "../../escape")
    with pytest.raises(ValueError, match="outside"):
        await GitWorkspaceManager(project / "workspaces").create(project, str(uuid4()))
    (project / "local.txt").write_text("local")
    with pytest.raises(ValueError, match="clean|dirty"):
        await manager.create(project, str(uuid4()))


async def test_refuses_symlinks_and_protected_files(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / "escape").symlink_to(project / "hello.txt")
    with pytest.raises(ValueError, match="symlink"):
        await manager.diff(workspace)
    (isolated / "escape").unlink()
    (isolated / ".env").write_text("SECRET=value")
    with pytest.raises(ValueError, match="protected"):
        await manager.diff(workspace)


async def test_diff_limit_and_forged_workspace(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    (Path(workspace["workspace_path"]) / "large.txt").write_text("x" * (manager.MAX_DIFF_BYTES + 1))
    with pytest.raises(ValueError, match="limit|large"):
        await manager.diff(workspace)
    forged = dict(workspace, workspace_path=str(project))
    with pytest.raises(ValueError, match="manifest|workspace"):
        await manager.diff(forged)


async def test_binary_deletion_and_staged_changes_preserve_worker_index(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / "hello.txt").unlink()
    (isolated / "binary.dat").write_bytes(b"\x00\xff\x01\x02")
    git(isolated, "add", "--all")
    original_status = git(isolated, "status", "--porcelain")
    reviewed = await manager.diff(workspace)
    assert git(isolated, "status", "--porcelain") == original_status
    await manager.accept(workspace, reviewed["digest"])
    assert not (project / "hello.txt").exists()
    assert (project / "binary.dat").read_bytes() == b"\x00\xff\x01\x02"
    assert git(isolated, "status", "--porcelain") == original_status


async def test_rejects_symlink_storage_nested_project_and_existing_branch(project, tmp_path):
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "real-storage")
    with pytest.raises(ValueError, match="symlink"):
        await GitWorkspaceManager(linked).create(project, str(uuid4()))
    nested = project / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="root"):
        await GitWorkspaceManager(tmp_path / "workspaces").create(nested, str(uuid4()))
    job = str(uuid4())
    branch = f"multiclaw/job-{job}"
    git(project, "branch", branch)
    with pytest.raises(ValueError, match="already exists"):
        await GitWorkspaceManager(tmp_path / "workspaces").create(project, job)
    assert git(project, "rev-parse", branch) == git(project, "rev-parse", "HEAD")


async def test_refuses_shell_configuration_and_secret_paths(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / ".bashrc").write_text("echo unsafe")
    with pytest.raises(ValueError, match="protected"):
        await manager.diff(workspace)


async def test_retry_create_after_branch_conflict_is_removed(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    job = str(uuid4())
    branch = f"multiclaw/job-{job}"
    git(project, "branch", branch)
    with pytest.raises(ValueError, match="already exists"):
        await manager.create(project, job)
    git(project, "branch", "-d", branch)
    workspace = await manager.create(project, job)
    assert Path(workspace["workspace_path"]).is_dir()
    assert await manager.create(project, job) == workspace


async def test_rejects_independently_initialized_nested_repository(project, tmp_path):
    nested = project / "nested"
    nested.mkdir()
    git(nested, "init")
    git(nested, "config", "user.email", "test@example.com")
    git(nested, "config", "user.name", "Test")
    git(nested, "commit", "--allow-empty", "-m", "nested initial")
    with pytest.raises(ValueError, match="nested|ancestor"):
        await GitWorkspaceManager(tmp_path / "workspaces").create(nested, str(uuid4()))


async def test_unchanged_protected_files_allow_unrelated_changes(project, tmp_path):
    (project / "credentials.json").write_text('{"existing": "secret"}')
    git(project, "add", "credentials.json")
    git(project, "commit", "-m", "existing protected file")
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / "hello.txt").write_text("after\n")
    reviewed = await manager.diff(workspace)
    assert reviewed["files"] == ["hello.txt"]
    await manager.accept(workspace, reviewed["digest"])
    assert (project / "hello.txt").read_text() == "after\n"
    assert (project / "credentials.json").read_text() == '{"existing": "secret"}'
    (isolated / "credentials.json").write_text('{"changed": "secret"}')
    with pytest.raises(ValueError, match="protected"):
        await manager.diff(workspace)


async def test_rejects_symlink_captured_between_path_check_and_index_snapshot(project, tmp_path, monkeypatch):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    original_git = manager._git

    def git_with_racing_symlink(root, *args, **kwargs):
        target = isolated / "hello.txt"
        if root == isolated and args[0] == "add":
            target.unlink()
            target.symlink_to(project / "hello.txt")
        output = original_git(root, *args, **kwargs)
        if root == isolated and args[0] == "write-tree":
            target.unlink()
            target.write_text("before\n")
        return output

    monkeypatch.setattr(manager, "_git", git_with_racing_symlink)
    with pytest.raises(ValueError, match="symlink"):
        await manager.diff(workspace)
    assert (project / "hello.txt").read_text() == "before\n"


async def test_operational_artifacts_are_excluded_from_cleanliness_diff_and_acceptance(project, tmp_path):
    operational = project / ".multiclaw"
    operational.mkdir()
    (operational / "tracked.txt").write_text("original runtime data")
    git(project, "add", ".multiclaw/tracked.txt")
    git(project, "commit", "-m", "existing runtime data")
    (operational / "tracked.txt").write_text("source runtime update")
    (operational / "source-result.txt").write_text("source artifact")
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    results = isolated / ".multiclaw" / "tool-results"
    results.mkdir()
    (results / "output.txt").write_text("generated tool result")
    (isolated / ".multiclaw" / "tracked.txt").write_text("worker runtime update")
    (isolated / "hello.txt").write_text("after\n")
    reviewed = await manager.diff(workspace)
    assert reviewed["files"] == ["hello.txt"]
    assert ".multiclaw" not in reviewed["patch"]
    await manager.accept(workspace, reviewed["digest"])
    assert (project / "hello.txt").read_text() == "after\n"
    assert (operational / "tracked.txt").read_text() == "source runtime update"
    assert not (operational / "tool-results" / "output.txt").exists()


async def test_unchanged_tracked_symlink_permits_unrelated_review_and_acceptance(project, tmp_path):
    (project / "existing-link").symlink_to(tmp_path / "outside")
    git(project, "add", "existing-link")
    git(project, "commit", "-m", "existing link")
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / "hello.txt").write_text("after\n")
    reviewed = await manager.diff(workspace)
    assert reviewed["files"] == ["hello.txt"]
    await manager.accept(workspace, reviewed["digest"])
    assert (project / "existing-link").is_symlink()
    assert (project / "hello.txt").read_text() == "after\n"
    (isolated / "existing-link").unlink()
    (isolated / "existing-link").symlink_to("hello.txt")
    with pytest.raises(ValueError, match="symlink"):
        await manager.diff(workspace)


async def test_rejects_new_embedded_repository_gitlink(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    embedded = Path(workspace["workspace_path"]) / "embedded"
    embedded.mkdir()
    git(embedded, "init")
    git(embedded, "config", "user.email", "test@example.com")
    git(embedded, "config", "user.name", "Test")
    git(embedded, "commit", "--allow-empty", "-m", "embedded repo")
    with pytest.raises(ValueError, match="gitlink|submodule|embedded"):
        await manager.diff(workspace)


async def test_accept_rechecks_source_after_patch_conflict_check(project, tmp_path, monkeypatch):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    (Path(workspace["workspace_path"]) / "hello.txt").write_text("after\n")
    reviewed = await manager.diff(workspace)
    original_git = manager._git

    def git_with_external_write(root, *args, **kwargs):
        output = original_git(root, *args, **kwargs)
        if root == project and args[:2] == ("apply", "--check"):
            (project / "external.txt").write_text("external writer")
        return output

    monkeypatch.setattr(manager, "_git", git_with_external_write)
    with pytest.raises(ValueError, match="clean|dirty"):
        await manager.accept(workspace, reviewed["digest"])
    assert (project / "hello.txt").read_text() == "before\n"
    assert (project / "external.txt").read_text() == "external writer"


async def test_discard_dirty_owned_workspace_preserves_source_and_is_idempotent(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    (isolated / "hello.txt").write_text("worker edit\n")
    (isolated / "new.txt").write_text("worker artifact")
    (project / "hello.txt").write_text("user edit\n")
    result = await manager.discard(workspace)
    assert result["discarded"]
    assert not isolated.parent.exists()
    assert (project / "hello.txt").read_text() == "user edit\n"
    assert not (project / "new.txt").exists()
    assert not git(project, "branch", "--list", workspace["branch"])
    assert (await manager.discard(workspace))["discarded"]


async def test_discard_refuses_foreign_manifest_path_and_branch(project, tmp_path):
    import json

    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    isolated = Path(workspace["workspace_path"])
    with pytest.raises(ValueError, match="workspace|manifest"):
        await manager.discard(dict(workspace, workspace_path=str(project)))
    assert isolated.exists()
    manifest = isolated.parent / "manifest.json"
    manifest.write_text(json.dumps(dict(workspace, branch="foreign")))
    with pytest.raises(ValueError, match="workspace|manifest"):
        await manager.discard(workspace)
    assert isolated.exists()
    assert (project / "hello.txt").read_text() == "before\n"


async def test_discard_retry_refuses_foreign_replacement_path(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    await manager.discard(workspace)
    isolated = Path(workspace["workspace_path"])
    isolated.mkdir(parents=True)
    (isolated / "foreign.txt").write_text("preserve")
    with pytest.raises(ValueError, match="workspace|manifest"):
        await manager.discard(workspace)
    assert (isolated / "foreign.txt").read_text() == "preserve"


async def test_discard_preserves_branch_used_by_another_worktree(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspace = await manager.create(project, str(uuid4()))
    other = tmp_path / "other-worktree"
    git(project, "worktree", "add", "--force", str(other), workspace["branch"])
    result = await manager.discard(workspace)
    assert result["branch_preserved"]
    assert other.exists()
    assert git(other, "symbolic-ref", "--short", "HEAD") == workspace["branch"]
    assert git(project, "branch", "--list", workspace["branch"])
    assert (project / "hello.txt").read_text() == "before\n"
    assert (await manager.discard(workspace))["discarded"]


async def test_batch_review_accepts_disjoint_changes_in_one_patch(project, tmp_path, monkeypatch):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    first = await manager.create(project, str(uuid4()))
    second = await manager.create(project, str(uuid4()))
    (Path(first["workspace_path"]) / "hello.txt").write_text("first member\n")
    (Path(second["workspace_path"]) / "second.txt").write_text("second member\n")
    reviewed = await manager.diff_batch([first, second])
    assert set(reviewed["files"]) == {"hello.txt", "second.txt"}
    assert await manager.diff_batch([second, first]) == reviewed
    original_git = manager._git
    applied = []

    def capture_apply(root, *args, **kwargs):
        if root == project and args[:2] == ("apply", "--binary"):
            applied.append(kwargs["data"])
        return original_git(root, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", capture_apply)
    result = await manager.accept_batch([second, first], reviewed["digest"])
    assert result["accepted"]
    assert applied == [reviewed["patch"].encode()]
    assert (project / "hello.txt").read_text() == "first member\n"
    assert (project / "second.txt").read_text() == "second member\n"
    assert git(project, "rev-parse", "HEAD") == first["base_commit"]


async def test_batch_overlap_rejected_without_source_changes(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    workspaces = [await manager.create(project, str(uuid4())) for _ in range(2)]
    for position, workspace in enumerate(workspaces):
        (Path(workspace["workspace_path"]) / "hello.txt").write_text(f"member {position}\n")
    with pytest.raises(ValueError, match="overlap"):
        await manager.diff_batch(workspaces)
    with pytest.raises(ValueError, match="overlap"):
        await manager.accept_batch(workspaces, "0" * 64)
    assert (project / "hello.txt").read_text() == "before\n"
    assert all(Path(workspace["workspace_path"]).exists() for workspace in workspaces)


async def test_batch_digest_binds_member_content_and_membership(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    first = await manager.create(project, str(uuid4()))
    second = await manager.create(project, str(uuid4()))
    (Path(first["workspace_path"]) / "hello.txt").write_text("reviewed\n")
    reviewed = await manager.diff_batch([first, second])
    with pytest.raises(ValueError, match="digest|review"):
        await manager.accept_batch([first], reviewed["digest"])
    (Path(second["workspace_path"]) / "second.txt").write_text("late member edit\n")
    with pytest.raises(ValueError, match="digest|review"):
        await manager.accept_batch([first, second], reviewed["digest"])
    assert (project / "hello.txt").read_text() == "before\n"
    assert not (project / "second.txt").exists()


@pytest.mark.parametrize("limit", ["bytes", "files"])
async def test_batch_combined_limits_reject_without_partial_acceptance(project, tmp_path, limit):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    first = await manager.create(project, str(uuid4()))
    second = await manager.create(project, str(uuid4()))
    (Path(first["workspace_path"]) / "first.txt").write_text("first\n")
    (Path(second["workspace_path"]) / "second.txt").write_text("second\n")
    individual = [await manager.diff(workspace) for workspace in [first, second]]
    if limit == "bytes":
        manager.MAX_DIFF_BYTES = max(len(diff["patch"].encode()) for diff in individual) + 1
    else:
        manager.MAX_FILES = 1
    with pytest.raises(ValueError, match="limit"):
        await manager.diff_batch([first, second])
    with pytest.raises(ValueError, match="limit"):
        await manager.accept_batch([first, second], "0" * 64)
    assert not (project / "first.txt").exists()
    assert not (project / "second.txt").exists()


async def test_batch_rejects_mixed_projects_bases_duplicate_and_empty(project, tmp_path):
    manager = GitWorkspaceManager(tmp_path / "workspaces")
    first = await manager.create(project, str(uuid4()))
    git(project, "commit", "--allow-empty", "-m", "new base")
    second = await manager.create(project, str(uuid4()))
    with pytest.raises(ValueError, match="base"):
        await manager.diff_batch([first, second])
    other = tmp_path / "other-project"
    git(tmp_path, "clone", str(project), str(other))
    third = await manager.create(other, str(uuid4()))
    with pytest.raises(ValueError, match="project"):
        await manager.diff_batch([second, third])
    with pytest.raises(ValueError, match="duplicate"):
        await manager.diff_batch([first, first])
    with pytest.raises(ValueError, match="at least one"):
        await manager.diff_batch([])
