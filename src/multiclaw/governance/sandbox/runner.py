from __future__ import annotations

import asyncio
import os
import signal

from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import SandboxExecResult, SandboxedLaunchSpec


class SandboxProcessRunner:
    def __init__(self, *, term_grace_seconds: float = 1.0) -> None:
        if term_grace_seconds <= 0:
            raise ValueError("term_grace_seconds must be positive")
        self._term_grace_seconds = term_grace_seconds

    async def run(
        self,
        spec: SandboxedLaunchSpec,
        timeout_seconds: float,
    ) -> SandboxExecResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        try:
            proc = await asyncio.create_subprocess_exec(
                spec.executable,
                *spec.args,
                stdin=asyncio.subprocess.PIPE if spec.stdin_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.cwd),
                env=dict(spec.env),
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxLaunchError(
                "failed to launch sandbox process "
                f"backend={spec.backend_name!r} profile={spec.profile_name!r} "
                f"executable={spec.executable!r} cwd={str(spec.cwd)!r}"
            ) from exc

        communicate_task = asyncio.create_task(proc.communicate(spec.stdin_bytes))

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=timeout_seconds,
            )
            return self._build_result(spec, proc.returncode, stdout, stderr, timed_out=False)
        except asyncio.TimeoutError:
            stdout, stderr = await self._cleanup_after_interrupt(
                proc,
                communicate_task,
                escalation_timeout=self._term_grace_seconds,
            )
            return self._build_result(spec, proc.returncode, stdout, stderr, timed_out=True)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._cleanup_after_interrupt(
                    proc,
                    communicate_task,
                    escalation_timeout=self._term_grace_seconds,
                )
            )
            raise

    async def _cleanup_after_interrupt(
        self,
        proc: asyncio.subprocess.Process,
        communicate_task: asyncio.Task[tuple[bytes | None, bytes | None]],
        *,
        escalation_timeout: float,
    ) -> tuple[bytes, bytes]:
        pgid = self._get_process_group_id(proc.pid)
        self._send_signal(proc, pgid, signal.SIGTERM)

        try:
            return await self._await_communicate(
                communicate_task,
                timeout=escalation_timeout,
            )
        except asyncio.TimeoutError:
            self._send_signal(proc, pgid, signal.SIGKILL)
            return await self._await_communicate(communicate_task)

    async def _await_communicate(
        self,
        communicate_task: asyncio.Task[tuple[bytes | None, bytes | None]],
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        try:
            waiter = asyncio.shield(communicate_task)
            if timeout is None:
                stdout, stderr = await waiter
            else:
                stdout, stderr = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            raise
        except Exception:
            if communicate_task.done():
                communicate_task.exception()
            raise
        return stdout or b"", stderr or b""

    def _send_signal(
        self,
        proc: asyncio.subprocess.Process,
        pgid: int | None,
        sig: signal.Signals,
    ) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            elif proc.returncode is None:
                os.kill(proc.pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            return

    def _get_process_group_id(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None
        except OSError:
            return None

    def _build_result(
        self,
        spec: SandboxedLaunchSpec,
        returncode: int | None,
        stdout: bytes | None,
        stderr: bytes | None,
        *,
        timed_out: bool,
    ) -> SandboxExecResult:
        exit_code, signal_name = self._decode_returncode(returncode)
        return SandboxExecResult(
            exit_code=exit_code,
            timed_out=timed_out,
            signal=signal_name,
            stdout=stdout or b"",
            stderr=stderr or b"",
            backend_name=spec.backend_name,
            profile_name=spec.profile_name,
            unsafe_fallback_used=spec.unsafe_fallback_used,
        )

    def _decode_returncode(self, returncode: int | None) -> tuple[int | None, str | None]:
        if returncode is None:
            return None, None
        if returncode >= 0:
            return returncode, None
        try:
            return None, signal.Signals(-returncode).name
        except ValueError:
            return None, f"SIG{-returncode}"
