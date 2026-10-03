"""Isolated Git workspaces and explicit, reviewed patch acceptance."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from uuid import UUID


class GitWorkspaceManager:
    MAX_DIFF_BYTES = 2 * 1024 * 1024
    MAX_FILES = 2000
    _USER_PATHSPEC = (".", ":(top,exclude).multiclaw")

    def __init__(self, storage_root: Path):
        self.storage_root = Path(storage_root).absolute()
        self._lock = threading.RLock()

    @staticmethod
    def _safe_absolute(path: Path) -> Path:
        path = Path(path).absolute()
        if path.resolve() != path:
            raise ValueError("Path contains a symlink or traversal escape")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError("Path contains a symlink")
        return path

    def _git(self, root: Path, *args: str, env: dict | None = None,
             data: bytes | None = None, limit: int | None = None) -> bytes:
        # Spooling prevents a large diff from being accumulated in memory.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                ["git", "-C", str(root), *args], input=data, stdout=output,
                stderr=errors, env=env, timeout=60, check=False,
            )
            output.seek(0, os.SEEK_END)
            if output.tell() > (limit or self.MAX_DIFF_BYTES):
                raise ValueError("Git output exceeds the configured size limit")
            output.seek(0)
            if result.returncode:
                errors.seek(0)
                raise ValueError(f"Git {args[0]} failed: {errors.read(4096).decode(errors='replace').strip()}")
            return output.read()

    def _project(self, path: Path) -> Path:
        root = self._safe_absolute(path)
        actual = Path(self._git(root, "rev-parse", "--show-toplevel").decode().strip())
        if actual.resolve() != root:
            raise ValueError("Project must be the real Git repository root")
        for ancestor in root.parents:
            if not (ancestor / ".git").exists():
                continue
            try:
                enclosing = Path(self._git(ancestor, "rev-parse", "--show-toplevel").decode().strip()).resolve()
            except ValueError:
                continue
            if root.is_relative_to(enclosing):
                raise ValueError("Project is a nested repository inside an ancestor Git worktree")
        return root

    def _clean(self, root: Path) -> None:
        if self._git(root, "status", "--porcelain", "--untracked-files=all", "--", *self._USER_PATHSPEC):
            raise ValueError("Source project must be clean (tracked and untracked files)")

    async def create(self, project_root: Path, job_id: str) -> dict:
        return await asyncio.to_thread(self._create, project_root, job_id)

    def _create(self, project_root: Path, job_id: str) -> dict:
        try:
            if str(UUID(job_id)) != job_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Job id must be a canonical UUID") from None
        with self._lock:
            root = self._project(project_root)
            storage = self._safe_absolute(self.storage_root)
            if storage.is_relative_to(root) or root.is_relative_to(storage):
                raise ValueError("Workspace storage must be outside the source project")
            job = storage / job_id
            manifest = job / "manifest.json"
            if job.exists():
                try:
                    workspace = json.loads(manifest.read_text())
                except (OSError, ValueError):
                    raise ValueError("Existing job has no valid workspace manifest") from None
                if workspace.get("project_root") != str(root):
                    raise ValueError("Workspace manifest belongs to another project")
                self._validate(workspace)
                return workspace
            self._clean(root)
            base = self._git(root, "rev-parse", "HEAD").decode().strip()
            branch = f"multiclaw/job-{job_id}"
            workspace = {"workspace_path": str(job / "repo"), "project_root": str(root),
                         "base_commit": base, "branch": branch, "job_id": job_id}
            job.mkdir(parents=True, exist_ok=False)
            created_directory = job.stat()
            # Git refuses both an existing branch and an occupied worktree path.
            try:
                self._git(root, "worktree", "add", "-b", branch, str(job / "repo"), base)
            except Exception:
                # Only reclaim our exact empty directory. Partial worktrees and
                # any concurrent writes remain available for inspection.
                try:
                    self._safe_absolute(job)
                    current_directory = job.stat()
                    if (current_directory.st_dev, current_directory.st_ino) == (
                        created_directory.st_dev, created_directory.st_ino
                    ):
                        job.rmdir()
                except (OSError, ValueError):
                    pass
                raise
            with manifest.open("x") as target:
                json.dump(workspace, target)
            return workspace

    def _validate(self, workspace: dict) -> tuple[Path, Path]:
        try:
            job_id = workspace["job_id"]
            if str(UUID(job_id)) != job_id:
                raise ValueError
            storage = self._safe_absolute(self.storage_root)
            job = self._safe_absolute(storage / job_id)
            isolated = self._safe_absolute(Path(workspace["workspace_path"]))
            root = self._project(Path(workspace["project_root"]))
            if isolated != job / "repo" or storage.is_relative_to(root) or root.is_relative_to(storage):
                raise ValueError
            manifest_path = self._safe_absolute(job / "manifest.json")
            if json.loads(manifest_path.read_text()) != workspace:
                raise ValueError
            common = Path(self._git(isolated, "rev-parse", "--git-common-dir").decode().strip())
            if not common.is_absolute():
                common = isolated / common
            source_common = Path(self._git(root, "rev-parse", "--git-common-dir").decode().strip())
            if not source_common.is_absolute():
                source_common = root / source_common
            if common.resolve() != source_common.resolve():
                raise ValueError
            if self._git(isolated, "symbolic-ref", "--short", "HEAD").decode().strip() != workspace["branch"]:
                raise ValueError
            self._git(isolated, "cat-file", "-e", workspace["base_commit"] + "^{commit}")
            return root, isolated
        except (KeyError, TypeError, OSError, ValueError) as error:
            raise ValueError(f"Invalid workspace manifest or path: {error}") from error

    @staticmethod
    def _safe_file(root: Path, name: str, *, check_protected: bool = True) -> None:
        path = Path(name)
        parts = [part.casefold() for part in path.parts]
        if path.is_absolute() or not parts or any(part in {"..", ".git"} for part in parts):
            raise ValueError(f"Unsafe file path: {name}")
        protected = {".env", ".aws", ".ssh", ".gnupg", ".bashrc", ".zshrc",
                     ".profile", "credentials", "credentials.json", "secrets.json"}
        if check_protected and any(part in protected or part.startswith(".env.") or part.endswith((".pem", ".key")) for part in parts):
            raise ValueError(f"Cannot accept protected file: {name}")
        for component in (root / path, *(root / path).parents):
            if component == root:
                break
            if component.is_symlink():
                raise ValueError(f"Cannot accept symlink path: {name}")
        if not (root / path).resolve().is_relative_to(root):
            raise ValueError(f"File escapes workspace: {name}")

    async def diff(self, workspace: dict) -> dict:
        return await asyncio.to_thread(self._diff, workspace)

    def _diff(self, workspace: dict) -> dict:
        with self._lock:
            root, isolated = self._validate(workspace)
            # A separate index captures staged, unstaged, committed, and new files
            # without changing the worker's index or committing its work.
            with tempfile.TemporaryDirectory(prefix="multiclaw-index-") as temporary:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"))
                self._git(isolated, "read-tree", workspace["base_commit"], env=env)
                candidates = self._git(isolated, "ls-files", "--others", "--exclude-standard", "-z", "--", *self._USER_PATHSPEC, env=env).split(b"\0")
                for candidate in candidates:
                    if candidate:
                        self._safe_file(isolated, os.fsdecode(candidate), check_protected=False)
                # Preserve the base tree's operational files, excluding both
                # generated artifacts and worker edits under .multiclaw.
                self._git(isolated, "add", "--all", "--", *self._USER_PATHSPEC, env=env)
                tree = self._git(isolated, "write-tree", env=env).decode().strip()
                # Immutable old/new modes catch capture races without traversing
                # unchanged symlinks or treating embedded repos as ordinary files.
                changes = self._git(isolated, "diff", "--raw", "--no-renames", "-z", workspace["base_commit"], tree).split(b"\0")
                files = []
                for position in range(0, len(changes) - 1, 2):
                    modes = changes[position].split()[:2]
                    name = os.fsdecode(changes[position + 1])
                    if any(mode.lstrip(b":") == b"120000" for mode in modes):
                        raise ValueError(f"Cannot accept captured symlink: {name}")
                    if any(mode.lstrip(b":") == b"160000" for mode in modes):
                        raise ValueError(f"Cannot accept gitlink or embedded submodule: {name}")
                    files.append(name)
                if len(files) > self.MAX_FILES:
                    raise ValueError("Diff exceeds the file count limit")
                for name in files:
                    self._safe_file(isolated, name)
                    self._safe_file(root, name)
                patch = self._git(isolated, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames", workspace["base_commit"], tree)
            return {"patch": patch.decode("utf-8", errors="surrogateescape"), "files": files,
                    "digest": hashlib.sha256(patch).hexdigest()}

    async def accept(self, workspace: dict, expected_digest: str) -> dict:
        """Apply the exact reviewed patch.

        Rechecks and Git's atomic conflict checking protect against observed
        source changes. External editors do not share our lock, so callers must
        coordinate source writes during the final check/apply interval.
        """
        return await asyncio.to_thread(self._accept, workspace, expected_digest)

    def _accept_source_ready(self, root: Path, workspace: dict, files: list[str]) -> None:
        self._clean(root)
        if self._git(root, "rev-parse", "HEAD").decode().strip() != workspace["base_commit"]:
            raise ValueError("Source base commit changed; review requires a new workspace")
        for name in files:
            self._safe_file(root, name)

    def _accept(self, workspace: dict, expected_digest: str) -> dict:
        with self._lock:
            root, _ = self._validate(workspace)
            reviewed = self._diff(workspace)
            if reviewed["digest"] != expected_digest:
                raise ValueError("Workspace changed since review: digest mismatch")
            self._accept_source_ready(root, workspace, reviewed["files"])
            patch = reviewed["patch"].encode("utf-8", errors="surrogateescape")
            if patch:
                self._git(root, "apply", "--check", "--binary", "-", data=patch)
                self._accept_source_ready(root, workspace, reviewed["files"])
                self._git(root, "apply", "--binary", "-", data=patch)
            return {"accepted": True, "files": reviewed["files"], "digest": reviewed["digest"]}

    def _batch_members(self, workspaces: list[dict]) -> tuple[list[dict], Path]:
        if not workspaces:
            raise ValueError("A batch requires at least one workspace")
        roots = []
        for workspace in workspaces:
            root, _ = self._validate(workspace)
            roots.append((root, workspace["base_commit"]))
        if len(set(roots)) != 1:
            raise ValueError("Batch workspaces must share the same project and base commit")
        ordered = sorted(workspaces, key=lambda workspace: workspace["job_id"])
        if len({workspace["job_id"] for workspace in ordered}) != len(ordered):
            raise ValueError("Batch contains duplicate workspace jobs")
        return ordered, roots[0][0]

    async def diff_batch(self, workspaces: list[dict]) -> dict:
        return await asyncio.to_thread(self._diff_batch, workspaces)

    def _diff_batch(self, workspaces: list[dict]) -> dict:
        with self._lock:
            ordered, root = self._batch_members(workspaces)
            patches = []
            files: set[str] = set()
            bindings = []
            total_bytes = 0
            for workspace in ordered:
                captured = self._diff(workspace)
                overlap = files.intersection(captured["files"])
                if overlap:
                    raise ValueError("Batch changes overlap; review overlapping files in separate assignments")
                files.update(captured["files"])
                patch = captured["patch"].encode("utf-8", errors="surrogateescape")
                total_bytes += len(patch)
                if total_bytes > self.MAX_DIFF_BYTES or len(files) > self.MAX_FILES:
                    raise ValueError("Combined batch diff exceeds the configured size or file count limit")
                patches.append(patch)
                bindings.append({"job_id": workspace["job_id"], "digest": captured["digest"],
                                 "base_commit": workspace["base_commit"], "project_root": str(root)})
            combined = b"".join(patches)
            header = json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()
            return {"patch": combined.decode("utf-8", errors="surrogateescape"),
                    "files": sorted(files), "digest": hashlib.sha256(header + b"\0" + combined).hexdigest()}

    async def accept_batch(self, workspaces: list[dict], expected_digest: str) -> dict:
        """Apply one aggregate reviewed patch; external writers need coordination."""
        return await asyncio.to_thread(self._accept_batch, workspaces, expected_digest)

    def _accept_batch(self, workspaces: list[dict], expected_digest: str) -> dict:
        with self._lock:
            ordered, root = self._batch_members(workspaces)
            reviewed = self._diff_batch(ordered)
            if reviewed["digest"] != expected_digest:
                raise ValueError("Batch changed since review: digest mismatch")
            self._accept_source_ready(root, ordered[0], reviewed["files"])
            patch = reviewed["patch"].encode("utf-8", errors="surrogateescape")
            if patch:
                self._git(root, "apply", "--check", "--binary", "-", data=patch)
                self._accept_source_ready(root, ordered[0], reviewed["files"])
                self._git(root, "apply", "--binary", "-", data=patch)
            return {"accepted": True, "files": reviewed["files"], "digest": reviewed["digest"]}

    async def discard(self, workspace: dict) -> dict:
        """Discard owned worktree data only after explicit deletion authorization."""
        return await asyncio.to_thread(self._discard, workspace)

    def _discard(self, workspace: dict) -> dict:
        with self._lock:
            # Even an idempotent retry must resolve precisely to our namespace.
            try:
                job_id = workspace["job_id"]
                if str(UUID(job_id)) != job_id:
                    raise ValueError
                storage = self._safe_absolute(self.storage_root)
                job = self._safe_absolute(storage / job_id)
                isolated = self._safe_absolute(Path(workspace["workspace_path"]))
                root = self._project(Path(workspace["project_root"]))
                branch = f"multiclaw/job-{job_id}"
                if (isolated != job / "repo" or workspace["branch"] != branch
                        or storage.is_relative_to(root) or root.is_relative_to(storage)):
                    raise ValueError
            except (KeyError, TypeError, AttributeError, ValueError, OSError) as error:
                raise ValueError(f"Invalid workspace manifest or discard path: {error}") from error
            if not job.exists():
                # No manifest remains to authorize deleting a branch or any
                # other path. Successful repeated cleanup therefore does nothing.
                return {"discarded": True}
            self._validate(workspace)
            if {entry.name for entry in job.iterdir()} != {"repo", "manifest.json"}:
                raise ValueError("Workspace job directory contains unexpected data; refusing discard")
            self._git(root, "worktree", "remove", "--force", str(isolated))
            worktrees = self._git(root, "worktree", "list", "--porcelain", "-z").split(b"\0")
            branch_used = f"branch refs/heads/{branch}".encode() in worktrees
            if not branch_used and self._git(root, "branch", "--list", branch):
                self._git(root, "branch", "-D", branch)
            manifest = self._safe_absolute(job / "manifest.json")
            if json.loads(manifest.read_text()) != workspace:
                raise ValueError("Workspace manifest changed during discard")
            manifest.unlink()
            job.rmdir()
            return {"discarded": True, "branch_preserved": branch_used}
