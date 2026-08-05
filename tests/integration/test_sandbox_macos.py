from __future__ import annotations

import os
import shlex
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterator

import pytest

from multiclaw.config.settings import SandboxSettings
from multiclaw.governance.sandbox.models import SandboxExecRequest, SandboxExecResult
from multiclaw.governance.sandbox.manager import SandboxManager
from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend
from multiclaw.tools.shell import ShellToolBuilder


def _native_skip_reason() -> str:
    if not sys.platform.startswith("darwin"):
        return "requires macOS"
    return "set MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 to run native sandbox tests"


def _native_skip_enabled() -> bool:
    if not sys.platform.startswith("darwin"):
        return True
    return os.environ.get("MULTICLAW_RUN_NATIVE_SANDBOX_TESTS") != "1"


pytestmark = [
    pytest.mark.native_sandbox,
    pytest.mark.macos_sandbox,
    pytest.mark.skipif(_native_skip_enabled(), reason=_native_skip_reason()),
]


def _require_backend_prerequisites() -> None:
    if not sys.platform.startswith("darwin"):
        return
    if os.environ.get("MULTICLAW_RUN_NATIVE_SANDBOX_TESTS") != "1":
        return
    backend = Path("/usr/bin/sandbox-exec")
    if not backend.is_file() or not os.access(backend, os.X_OK):
        pytest.fail(f"missing executable macOS sandbox backend: {backend}", pytrace=False)


@dataclass(frozen=True)
class SandboxTree:
    workspace: Path
    allowed_dir: Path
    git_protected: Path
    env_secret_path: Path
    env_secret_value: str
    outside_dir: Path
    outside_sentinel_path: Path
    outside_sentinel_value: str
    timeout_pids: Path


class ParentTcpListener:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self._socket.settimeout(0.05)
        self.port = self._socket.getsockname()[1]
        self._accepted = False

    def accepted_connection(self, *, timeout_seconds: float = 0.4) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                conn, _ = self._socket.accept()
            except TimeoutError:
                continue
            except socket.timeout:
                continue
            self._accepted = True
            conn.close()
            return True
        return self._accepted

    def close(self) -> None:
        self._socket.close()


def _manager_readiness_detail(manager: SandboxManager) -> str:
    readiness = manager.finalize_readiness()
    return (
        f"ready={readiness.ready} "
        f"backend={readiness.backend_name} "
        f"probe_available={readiness.probe.available} "
        f"probe_reason={readiness.probe.reason!r} "
        f"profiles={dict(readiness.profiles)!r} "
        f"capabilities={dict(readiness.probe.capabilities)!r}"
    )


def _shell_exit_code(result) -> int:
    assert result.status == "success", result.content
    return int(result.data["exit_code"])


def _python_exit_code(result: SandboxExecResult) -> int:
    assert result.exit_code is not None
    assert result.timed_out is False
    assert result.signal is None
    return result.exit_code


def _python_stdout_text(result: SandboxExecResult) -> str:
    return result.stdout.decode("utf-8", errors="strict")


def _assert_denied_marker(
    result: SandboxExecResult,
    *,
    prefix: str,
    expected_errnos: set[int] | None = None,
) -> int:
    assert _python_exit_code(result) == 0
    stdout = _python_stdout_text(result)
    match = re.fullmatch(rf"{re.escape(prefix)}:(\d+)", stdout)
    assert match is not None, stdout
    denied_errno = int(match.group(1))
    if expected_errnos is not None:
        assert denied_errno in expected_errnos
    assert result.stderr == b""
    return denied_errno


def _wait_for_pid_record(path: Path, *, timeout_seconds: float = 2.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(lines) == 2:
                return int(lines[0]), int(lines[1])
            last_error = AssertionError(f"unexpected pid record contents: {lines!r}")
        except Exception as exc:  # pragma: no cover - exercised in polling loop
            last_error = exc
        time.sleep(0.02)
    detail = f"; last error: {last_error}" if last_error is not None else ""
    raise AssertionError(f"timed out waiting for pid record at {path}{detail}")


def _process_missing(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _assert_pid_gone(pid: int, *, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _process_missing(pid):
            return
        time.sleep(0.05)
    os.kill(pid, 0)


async def _run_shell(builder: ShellToolBuilder, command: str, **kwargs):
    invocation = builder.build(builder.validate({"command": command, **kwargs}))
    return await invocation.execute()


async def _run_manager_python(
    sandbox_manager: SandboxManager,
    sandbox_tree: SandboxTree,
    code: str,
    *,
    timeout_seconds: float = 5.0,
) -> SandboxExecResult:
    request = SandboxExecRequest(
        tool_name="native-python-probe",
        profile_name="code_exec_python",
        mode="exec_argv",
        argv=(str(Path(sys.executable).resolve()), "-I", "-S", "-c", code),
        workspace_root=sandbox_tree.workspace.resolve(),
        cwd=sandbox_tree.workspace.resolve(),
        timeout_seconds=timeout_seconds,
    )
    return await sandbox_manager.run(request)


async def _assert_direct_python_probe_control(
    sandbox_manager: SandboxManager,
    sandbox_tree: SandboxTree,
) -> None:
    result = await _run_manager_python(
        sandbox_manager,
        sandbox_tree,
        "import sys\n"
        "sys.stdout.write('PYTHON_VISIBLE')\n"
        "raise SystemExit(0)\n",
    )

    assert _python_exit_code(result) == 0
    assert _python_stdout_text(result) == "PYTHON_VISIBLE"
    assert result.stderr == b""


@pytest.fixture(scope="module")
def sandbox_tree(tmp_path_factory: pytest.TempPathFactory) -> SandboxTree:
    root = tmp_path_factory.mktemp("native-macos-sandbox")
    workspace = root / "workspace"
    allowed_dir = workspace / "allowed"
    outside_dir = root / "outside"
    git_dir = workspace / ".git"
    allowed_dir.mkdir(parents=True)
    outside_dir.mkdir()
    git_dir.mkdir()
    git_protected = git_dir / "protected"
    git_protected.write_text("protected\n", encoding="utf-8")
    env_secret_path = workspace / ".env"
    env_secret_value = "MACOS_NATIVE_SECRET_SENTINEL"
    env_secret_path.write_text(f"SECRET={env_secret_value}\n", encoding="utf-8")
    outside_sentinel_path = outside_dir / "outside-sentinel.txt"
    outside_sentinel_value = "MACOS_OUTSIDE_SENTINEL"
    outside_sentinel_path.write_text(outside_sentinel_value, encoding="utf-8")
    return SandboxTree(
        workspace=workspace,
        allowed_dir=allowed_dir,
        git_protected=git_protected,
        env_secret_path=env_secret_path,
        env_secret_value=env_secret_value,
        outside_dir=outside_dir,
        outside_sentinel_path=outside_sentinel_path,
        outside_sentinel_value=outside_sentinel_value,
        timeout_pids=allowed_dir / "timeout-pids.txt",
    )


@pytest.fixture
def parent_tcp_listener() -> Iterator[ParentTcpListener]:
    listener = ParentTcpListener()
    try:
        yield listener
    finally:
        listener.close()


@pytest.fixture(scope="module")
def sandbox_manager(sandbox_tree: SandboxTree) -> Iterator[SandboxManager]:
    _require_backend_prerequisites()
    manager = SandboxManager.create(
        settings=SandboxSettings(),
        debug=False,
        workspace_root=sandbox_tree.workspace,
        platform_name="Darwin",
        backend_override=SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")),
    )
    try:
        manager.initialize()
        if not manager.finalize_readiness().ready:
            pytest.fail(
                "macOS native sandbox readiness gate failed: " + _manager_readiness_detail(manager),
                pytrace=False,
            )
        yield manager
    finally:
        manager.close()


@pytest.fixture(scope="module")
def shell_builder(sandbox_tree: SandboxTree, sandbox_manager: SandboxManager) -> ShellToolBuilder:
    return ShellToolBuilder(
        sandbox_tree.workspace,
        sandbox_controller=sandbox_manager,
        profile_name="shell_workspace",
    )


@pytest.mark.asyncio
async def test_macos_sandbox_allows_workspace_write(
    sandbox_tree: SandboxTree,
    shell_builder: ShellToolBuilder,
) -> None:
    target = sandbox_tree.allowed_dir / "workspace-write.txt"

    result = await _run_shell(
        shell_builder,
        "printf 'workspace-ok' > "
        + shlex.quote(str(target))
        + " && cat "
        + shlex.quote(str(target)),
    )

    assert _shell_exit_code(result) == 0
    assert target.read_text(encoding="utf-8") == "workspace-ok"
    assert "workspace-ok" in result.content


@pytest.mark.asyncio
async def test_macos_sandbox_blocks_write_outside_workspace(
    sandbox_tree: SandboxTree,
    shell_builder: ShellToolBuilder,
) -> None:
    result = await _run_shell(
        shell_builder,
        "printf 'blocked' > " + shlex.quote(str(sandbox_tree.outside_sentinel_path)),
    )

    assert _shell_exit_code(result) != 0
    assert (
        sandbox_tree.outside_sentinel_path.read_text(encoding="utf-8")
        == sandbox_tree.outside_sentinel_value
    )


@pytest.mark.asyncio
async def test_macos_sandbox_blocks_git_writes(
    sandbox_tree: SandboxTree,
    shell_builder: ShellToolBuilder,
) -> None:
    original = sandbox_tree.git_protected.read_text(encoding="utf-8")

    result = await _run_shell(
        shell_builder,
        "printf 'mutated' > " + shlex.quote(str(sandbox_tree.git_protected)),
    )

    assert _shell_exit_code(result) != 0
    assert sandbox_tree.git_protected.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_macos_sandbox_hides_dotenv_reads(
    sandbox_tree: SandboxTree,
    shell_builder: ShellToolBuilder,
) -> None:
    result = await _run_shell(
        shell_builder,
        "cat " + shlex.quote(str(sandbox_tree.env_secret_path)),
    )

    assert _shell_exit_code(result) != 0
    assert sandbox_tree.env_secret_value not in result.content


@pytest.mark.asyncio
async def test_macos_sandbox_blocks_parent_listener_network(
    sandbox_manager: SandboxManager,
    sandbox_tree: SandboxTree,
    parent_tcp_listener: ParentTcpListener,
) -> None:
    await _assert_direct_python_probe_control(sandbox_manager, sandbox_tree)
    result = await _run_manager_python(
        sandbox_manager,
        sandbox_tree,
        "import sys\n"
        "try:\n"
        "    import socket\n"
        "except Exception:\n"
        "    raise SystemExit(41)\n"
        "try:\n"
        f"    sock = socket.create_connection(('127.0.0.1', {parent_tcp_listener.port}), timeout=1.0)\n"
        "except OSError as exc:\n"
        "    if exc.errno is None:\n"
        "        raise SystemExit(43)\n"
        "    sys.stdout.write(f'NETWORK_DENIED:{exc.errno}')\n"
        "    raise SystemExit(0)\n"
        "else:\n"
        "    sock.close()\n"
        "    raise SystemExit(42)\n",
    )

    _assert_denied_marker(result, prefix="NETWORK_DENIED")
    assert parent_tcp_listener.accepted_connection() is False


@pytest.mark.asyncio
async def test_macos_code_exec_blocks_child_process_creation(
    sandbox_manager: SandboxManager,
    sandbox_tree: SandboxTree,
) -> None:
    await _assert_direct_python_probe_control(sandbox_manager, sandbox_tree)
    result = await _run_manager_python(
        sandbox_manager,
        sandbox_tree,
        "try:\n"
        "    import errno\n"
        "    import subprocess\n"
        "    import sys\n"
        "except Exception:\n"
        "    raise SystemExit(51)\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-I', '-S', '-c', 'raise SystemExit(0)'], check=True)\n"
        "except OSError as exc:\n"
        "    if exc.errno in (errno.EPERM, errno.EACCES):\n"
        "        sys.stdout.write(f'CHILD_DENIED:{exc.errno}')\n"
        "        raise SystemExit(0)\n"
        "    raise SystemExit(53)\n"
        "except subprocess.CalledProcessError:\n"
        "    raise SystemExit(54)\n"
        "raise SystemExit(52)\n",
    )

    _assert_denied_marker(
        result,
        prefix="CHILD_DENIED",
        expected_errnos={1, 13},
    )


@pytest.mark.asyncio
async def test_macos_timed_out_shell_leaves_no_descendants(
    sandbox_tree: SandboxTree,
    shell_builder: ShellToolBuilder,
) -> None:
    if sandbox_tree.timeout_pids.exists():
        sandbox_tree.timeout_pids.unlink()

    result = await _run_shell(
        shell_builder,
        "trap '' TERM; "
        "/bin/sh -c 'trap \"\" TERM; sleep 60' & "
        "child=$!; "
        "printf '%s\\n%s\\n' \"$$\" \"$child\" > "
        + shlex.quote(str(sandbox_tree.timeout_pids))
        + "; "
        "wait \"$child\"",
        timeout=0.2,
    )
    shell_pid, child_pid = _wait_for_pid_record(sandbox_tree.timeout_pids)

    assert result.status == "success"
    assert "[Command timed out after 0s]" in result.content
    assert result.data == {"exit_code": -1}
    _assert_pid_gone(shell_pid)
    _assert_pid_gone(child_pid)
