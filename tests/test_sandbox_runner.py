import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import textwrap
import time

import pytest

from multiclaw.governance import SandboxLaunchError, SandboxedLaunchSpec
from multiclaw.governance.sandbox.runner import SandboxProcessRunner


pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals")


def _make_spec(
    tmp_path: Path,
    *,
    code: str,
    stdin_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
    private_root: Path | None = None,
    backend_name: str = "fake",
    profile_name: str = "test",
    correlation_id: str = "corr",
    unsafe_fallback_used: bool = False,
    cwd: Path | None = None,
    executable: str | None = None,
) -> SandboxedLaunchSpec:
    return SandboxedLaunchSpec(
        executable=executable or sys.executable,
        args=("-c", code),
        cwd=cwd or tmp_path,
        env=env or dict(os.environ),
        stdin_bytes=stdin_bytes,
        private_root=private_root or tmp_path,
        backend_name=backend_name,
        profile_name=profile_name,
        correlation_id=correlation_id,
        unsafe_fallback_used=unsafe_fallback_used,
    )


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


def _best_effort_kill_pid(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _best_effort_kill_pgid(pgid: int | None) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _is_pid_record(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("parent_pid"), int)
        and isinstance(value.get("child_pid"), int)
    )


async def _wait_for_pid_record(
    path: Path,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, int]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            last_error = exc
        else:
            if _is_pid_record(payload):
                return payload
            last_error = AssertionError(
                f"{path} contained unexpected payload: {payload!r}"
            )
        await asyncio.sleep(0.01)
    detail = f"; last error: {last_error}" if last_error is not None else ""
    raise AssertionError(f"timed out waiting for complete PID record at {path}{detail}")


@pytest.mark.asyncio
async def test_wait_for_pid_record_waits_for_complete_json(tmp_path: Path) -> None:
    record_path = tmp_path / "partial.json"

    async def writer() -> None:
        record_path.write_text("{", encoding="utf-8")
        await asyncio.sleep(0.05)
        record_path.write_text(
            json.dumps({"parent_pid": 111, "child_pid": 222}),
            encoding="utf-8",
        )

    writer_task = asyncio.create_task(writer())
    try:
        record = await _wait_for_pid_record(record_path, timeout_seconds=1.0)
    finally:
        await writer_task

    assert record == {"parent_pid": 111, "child_pid": 222}


@pytest.mark.asyncio
async def test_sandbox_runner_captures_success_and_metadata(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code=(
            "import sys; "
            "sys.stdout.write('hello stdout'); "
            "sys.stderr.write('hello stderr')"
        ),
        backend_name="fake-backend",
        profile_name="shell-profile",
        correlation_id="success",
        unsafe_fallback_used=True,
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.signal is None
    assert result.stdout == b"hello stdout"
    assert result.stderr == b"hello stderr"
    assert result.backend_name == "fake-backend"
    assert result.profile_name == "shell-profile"
    assert result.unsafe_fallback_used is True


@pytest.mark.asyncio
async def test_sandbox_runner_passes_stdin_bytes(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code=(
            "import sys; "
            "data = sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(data[::-1]); "
            "sys.stderr.buffer.write(b'stderr-bytes')"
        ),
        stdin_bytes=b"abcdef",
        correlation_id="stdin",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.exit_code == 0
    assert result.stdout == b"fedcba"
    assert result.stderr == b"stderr-bytes"


@pytest.mark.asyncio
async def test_sandbox_runner_treats_broken_pipe_on_stdin_as_nonfatal(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code="import sys; sys.exit(0)",
        stdin_bytes=b"x" * 200_000,
        correlation_id="stdin-broken-pipe",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.exit_code == 0
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_sandbox_runner_allows_exact_output_limit_boundary(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code="import sys; sys.stdout.buffer.write(b'a' * (128 * 1024))",
        correlation_id="stdout-boundary",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.completion_state == "completed"
    assert result.output_limit_stream is None
    assert len(result.stdout) == 128 * 1024
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_sandbox_runner_returns_output_limit_result_when_communicate_finishes_after_latch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec(
        tmp_path,
        code="print('unused')",
        correlation_id="communicate-done-overflow-latched",
    )

    class _FakeProcess:
        pid = 43210
        returncode = 0
        stdout = None
        stderr = None
        stdin = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    async def fake_communicate_with_limits(
        self,
        proc,
        stdin_bytes,
        captured_output,
        output_limit_event,
    ):
        del self, proc, stdin_bytes
        captured_output.mark_output_limit_exceeded("stdout")
        output_limit_event.set()
        return b"", b""

    real_wait = asyncio.wait

    async def fake_wait(tasks, *, timeout=None, return_when=asyncio.FIRST_COMPLETED):
        done, pending = await real_wait(tasks, timeout=timeout, return_when=return_when)
        communicate_task = next(task for task in tasks if task is not None and task.done())
        return {communicate_task}, pending

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        SandboxProcessRunner,
        "_communicate_with_limits",
        fake_communicate_with_limits,
    )
    monkeypatch.setattr(SandboxProcessRunner, "_get_process_group_id", lambda self, pid: pid)
    monkeypatch.setattr(asyncio, "wait", fake_wait)

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.completion_state == "output_limit_exceeded"
    assert result.output_limit_stream == "stdout"
    assert result.timed_out is False
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_sandbox_runner_clears_captured_output_when_stdout_limit_exceeded(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code=(
            "import sys; "
            "sys.stdout.buffer.write(b'a' * (128 * 1024 + 1)); "
            "sys.stderr.write('should not leak')"
        ),
        correlation_id="stdout-overflow",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.completion_state == "output_limit_exceeded"
    assert result.output_limit_stream == "stdout"
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_sandbox_runner_clears_captured_output_when_stderr_limit_exceeded(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code=(
            "import sys; "
            "sys.stderr.buffer.write(b'e' * (128 * 1024 + 1)); "
            "sys.stdout.write('should not leak')"
        ),
        correlation_id="stderr-overflow",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.completion_state == "output_limit_exceeded"
    assert result.output_limit_stream == "stderr"
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_sandbox_runner_returns_non_zero_exit_code(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code="import sys; sys.stderr.write('boom'); raise SystemExit(7)",
        correlation_id="non-zero",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.exit_code == 7
    assert result.timed_out is False
    assert result.signal is None
    assert result.stderr == b"boom"


@pytest.mark.asyncio
async def test_sandbox_runner_maps_signal_exit_to_signal_name(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code="import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        correlation_id="signal-exit",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.exit_code is None
    assert result.timed_out is False
    assert result.signal == "SIGTERM"


@pytest.mark.asyncio
async def test_sandbox_runner_marks_term_responsive_timeout(tmp_path: Path) -> None:
    spec = _make_spec(
        tmp_path,
        code="import time; time.sleep(60)",
        correlation_id="term-timeout",
    )

    result = await SandboxProcessRunner(term_grace_seconds=0.1).run(spec, 0.05)

    assert result.exit_code is None
    assert result.timed_out is True
    assert result.signal == "SIGTERM"


@pytest.mark.asyncio
async def test_sandbox_runner_escalates_to_kill_when_term_ignored(
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / "term-ignored-ready"
    spec = _make_spec(
        tmp_path,
        code=textwrap.dedent(
            f"""
            import signal
            import time
            from pathlib import Path

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path({str(ready_path)!r}).write_text("ready", encoding="utf-8")
            time.sleep(60)
            """
        ),
        correlation_id="kill-timeout",
    )

    await asyncio.to_thread(ready_path.unlink, missing_ok=True)
    runner_task = asyncio.create_task(
        SandboxProcessRunner(term_grace_seconds=0.05).run(spec, 1.0)
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if ready_path.exists():
            break
        if runner_task.done():
            break
        await asyncio.sleep(0.01)
    else:
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        raise AssertionError("timed out waiting for SIGTERM ignore handler readiness")

    if not ready_path.exists():
        result = await runner_task
        raise AssertionError(f"runner finished before readiness marker was written: {result!r}")

    result = await runner_task

    assert result.exit_code is None
    assert result.timed_out is True
    assert result.signal == "SIGKILL"


@pytest.mark.asyncio
async def test_sandbox_runner_prefers_output_limit_over_timeout_when_overflow_happens_during_cleanup(
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / "overflow-ready"
    spec = _make_spec(
        tmp_path,
        code=textwrap.dedent(
            f"""
            import os
            import signal
            import time
            from pathlib import Path

            triggered = False

            def on_term(signum, frame):
                global triggered
                triggered = True

            signal.signal(signal.SIGTERM, on_term)
            Path({str(ready_path)!r}).write_text("ready", encoding="utf-8")
            while not triggered:
                time.sleep(0.01)
            os.write(1, b"x" * (128 * 1024 + 1))
            time.sleep(60)
            """
        ),
        correlation_id="overflow-during-cleanup",
    )

    runner_task = asyncio.create_task(
        SandboxProcessRunner(term_grace_seconds=0.05).run(spec, 1.0)
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if ready_path.exists():
            break
        if runner_task.done():
            break
        await asyncio.sleep(0.01)
    else:
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        raise AssertionError("timed out waiting for overflow handler readiness")

    if not ready_path.exists():
        result = await runner_task
        raise AssertionError(f"runner finished before overflow readiness marker was written: {result!r}")

    result = await runner_task

    assert result.timed_out is False
    assert result.completion_state == "output_limit_exceeded"
    assert result.output_limit_stream == "stdout"
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_sandbox_runner_kills_descendants_on_timeout(tmp_path: Path) -> None:
    record_path = tmp_path / "pids.json"
    script = textwrap.dedent(
        f"""
        import json
        import subprocess
        import sys
        import time
        from pathlib import Path

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        Path({str(record_path)!r}).write_text(
            json.dumps({{"parent_pid": child.pid, "child_pid": __import__("os").getpid()}}),
            encoding="utf-8",
        )
        time.sleep(60)
        """
    )
    spec = _make_spec(tmp_path, code=script, correlation_id="descendant-timeout")

    task: asyncio.Task | None = None
    record: dict[str, int] | None = None
    child_pid: int | None = None
    parent_pid: int | None = None
    try:
        task = asyncio.create_task(
            SandboxProcessRunner(term_grace_seconds=0.05).run(spec, 1.0)
        )
        record = await _wait_for_pid_record(record_path)
        result = await task
        parent_pid = record["parent_pid"]
        child_pid = record["child_pid"]

        assert result.timed_out is True
        assert result.signal in {"SIGTERM", "SIGKILL"}
        _assert_pid_gone(parent_pid)
        _assert_pid_gone(child_pid)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if record is not None:
            parent_pid = record["parent_pid"]
            child_pid = record["child_pid"]
        _best_effort_kill_pid(parent_pid)
        _best_effort_kill_pid(child_pid)


@pytest.mark.asyncio
async def test_sandbox_runner_output_limit_kills_descendants_even_if_parent_exits(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "overflow-descendant-pids.json"
    child_code = textwrap.dedent(
        """
        import os
        import time

        os.write(1, b"x" * (128 * 1024 + 1))
        time.sleep(60)
        """
    ).strip()
    script = textwrap.dedent(
        f"""
        import json
        import subprocess
        import sys
        from pathlib import Path
        import os

        child = subprocess.Popen(
            [sys.executable, "-c", {child_code!r}],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        Path({str(record_path)!r}).write_text(
            json.dumps({{"parent_pid": os.getpid(), "child_pid": child.pid}}),
            encoding="utf-8",
        )
        raise SystemExit(0)
        """
    )
    spec = _make_spec(tmp_path, code=script, correlation_id="overflow-parent-exits")

    task = asyncio.create_task(SandboxProcessRunner(term_grace_seconds=0.05).run(spec, 1.0))
    record: dict[str, int] | None = None
    parent_pid: int | None = None
    child_pid: int | None = None

    try:
        record = await _wait_for_pid_record(record_path)
        parent_pid = record["parent_pid"]
        child_pid = record["child_pid"]
        result = await asyncio.wait_for(task, timeout=2.0)

        assert result.completion_state == "output_limit_exceeded"
        assert result.output_limit_stream == "stdout"
        assert result.stdout == b""
        assert result.stderr == b""
        _assert_pid_gone(child_pid)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if record is not None:
            parent_pid = record["parent_pid"]
            child_pid = record["child_pid"]
        _best_effort_kill_pgid(parent_pid)
        _best_effort_kill_pid(parent_pid)
        _best_effort_kill_pid(child_pid)


@pytest.mark.asyncio
async def test_sandbox_runner_cleans_up_on_cancellation(tmp_path: Path) -> None:
    record_path = tmp_path / "cancel-pids.json"
    script = textwrap.dedent(
        f"""
        import json
        import subprocess
        import sys
        import time
        from pathlib import Path

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        Path({str(record_path)!r}).write_text(
            json.dumps({{"parent_pid": child.pid, "child_pid": __import__("os").getpid()}}),
            encoding="utf-8",
        )
        time.sleep(60)
        """
    )
    spec = _make_spec(tmp_path, code=script, correlation_id="cancel")

    task = asyncio.create_task(SandboxProcessRunner(term_grace_seconds=0.05).run(spec, 60.0))
    record: dict[str, int] | None = None
    parent_pid: int | None = None
    child_pid: int | None = None

    try:
        record = await _wait_for_pid_record(record_path)
        parent_pid = record["parent_pid"]
        child_pid = record["child_pid"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_pid_gone(parent_pid)
        _assert_pid_gone(child_pid)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if record is not None:
            parent_pid = record["parent_pid"]
            child_pid = record["child_pid"]
        _best_effort_kill_pid(parent_pid)
        _best_effort_kill_pid(child_pid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "expected_fragment"),
    [
        pytest.param(
            lambda tmp_path: _make_spec(
                tmp_path,
                code="print('unused')",
                executable="/definitely/missing/executable",
                env={"SECRET_TOKEN": "top-secret"},
                stdin_bytes=b"super-secret-stdin",
                backend_name="missing-backend",
                profile_name="missing-profile",
                correlation_id="missing-executable",
            ),
            "missing-backend",
            id="missing-executable",
        ),
        pytest.param(
            lambda tmp_path: _make_spec(
                tmp_path,
                code="print('unused')",
                cwd=tmp_path / "missing-dir",
                env={"SECRET_TOKEN": "top-secret"},
                stdin_bytes=b"super-secret-stdin",
                backend_name="cwd-backend",
                profile_name="cwd-profile",
                correlation_id="missing-cwd",
            ),
            "cwd-backend",
            id="missing-cwd",
        ),
    ],
)
async def test_sandbox_runner_wraps_pre_spawn_failures(
    tmp_path: Path,
    spec,
    expected_fragment: str,
) -> None:
    with pytest.raises(SandboxLaunchError) as excinfo:
        await SandboxProcessRunner().run(spec(tmp_path), 1.0)

    message = str(excinfo.value)
    assert expected_fragment in message
    assert "SECRET_TOKEN" not in message
    assert "top-secret" not in message
    assert "super-secret-stdin" not in message


@pytest.mark.asyncio
async def test_sandbox_runner_does_not_delete_private_root(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    spec = _make_spec(
        tmp_path,
        code="print('ok')",
        private_root=private_root,
        correlation_id="private-root",
    )

    result = await SandboxProcessRunner().run(spec, 1.0)

    assert result.exit_code == 0
    assert private_root.exists()
    assert private_root.is_dir()


def test_sandbox_runner_requires_positive_term_grace_seconds() -> None:
    with pytest.raises(ValueError, match="term_grace_seconds"):
        SandboxProcessRunner(term_grace_seconds=0)


@pytest.mark.asyncio
async def test_sandbox_runner_requires_positive_timeout_seconds(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, code="print('unused')", correlation_id="bad-timeout")

    with pytest.raises(ValueError, match="timeout_seconds"):
        await SandboxProcessRunner().run(spec, 0)
