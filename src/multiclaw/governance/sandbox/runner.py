from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass

from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import SandboxExecResult, SandboxedLaunchSpec

MAX_CAPTURE_BYTES_PER_STREAM = 128 * 1024
READ_CHUNK_SIZE = 64 * 1024


@dataclass
class _CapturedOutput:
    stdout: bytearray
    stderr: bytearray
    output_limit_stream: str | None = None

    def buffer_for(self, stream_name: str) -> bytearray:
        if stream_name == "stdout":
            return self.stdout
        return self.stderr

    def mark_output_limit_exceeded(self, stream_name: str) -> None:
        if self.output_limit_stream is not None:
            return
        self.output_limit_stream = stream_name
        self.stdout.clear()
        self.stderr.clear()


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
        process_group_id = self._preserve_process_group_id(proc)

        captured_output = _CapturedOutput(stdout=bytearray(), stderr=bytearray())
        output_limit_event = asyncio.Event()
        communicate_task = asyncio.create_task(
            self._communicate_with_limits(
                proc,
                spec.stdin_bytes,
                captured_output,
                output_limit_event,
            )
        )
        output_limit_task = asyncio.create_task(output_limit_event.wait())

        try:
            done, _ = await asyncio.wait(
                {communicate_task, output_limit_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if communicate_task in done:
                stdout, stderr = await self._await_communicate(communicate_task)
                if captured_output.output_limit_stream is not None:
                    return self._build_result_for_completion(
                        spec,
                        proc.returncode,
                        stdout,
                        stderr,
                        captured_output=captured_output,
                        timed_out=False,
                    )
                return self._build_result(spec, proc.returncode, stdout, stderr, timed_out=False)
            if output_limit_task in done:
                stdout, stderr = await self._cleanup_after_interrupt(
                    proc,
                    communicate_task,
                    process_group_id,
                    escalation_timeout=self._term_grace_seconds,
                )
                return self._build_result_for_completion(
                    spec,
                    proc.returncode,
                    stdout,
                    stderr,
                    captured_output=captured_output,
                    timed_out=False,
                )

            stdout, stderr = await self._cleanup_after_interrupt(
                proc,
                communicate_task,
                process_group_id,
                escalation_timeout=self._term_grace_seconds,
            )
            return self._build_result_for_completion(
                spec,
                proc.returncode,
                stdout,
                stderr,
                captured_output=captured_output,
                timed_out=True,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._cleanup_after_interrupt(
                    proc,
                    communicate_task,
                    process_group_id,
                    escalation_timeout=self._term_grace_seconds,
                )
            )
            raise
        finally:
            output_limit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await output_limit_task

    async def _cleanup_after_interrupt(
        self,
        proc: asyncio.subprocess.Process,
        communicate_task: asyncio.Task[tuple[bytes | None, bytes | None]],
        process_group_id: int,
        *,
        escalation_timeout: float,
    ) -> tuple[bytes, bytes]:
        self._send_signal(proc, process_group_id, signal.SIGTERM)

        try:
            return await self._await_communicate(
                communicate_task,
                timeout=escalation_timeout,
            )
        except asyncio.TimeoutError:
            self._send_signal(proc, process_group_id, signal.SIGKILL)
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

    async def _communicate_with_limits(
        self,
        proc: asyncio.subprocess.Process,
        stdin_bytes: bytes | None,
        captured_output: _CapturedOutput,
        output_limit_event: asyncio.Event,
    ) -> tuple[bytes, bytes]:
        stdout_task = asyncio.create_task(
            self._read_stream(
                proc.stdout,
                stream_name="stdout",
                captured_output=captured_output,
                output_limit_event=output_limit_event,
            )
        )
        stderr_task = asyncio.create_task(
            self._read_stream(
                proc.stderr,
                stream_name="stderr",
                captured_output=captured_output,
                output_limit_event=output_limit_event,
            )
        )
        stdin_task = asyncio.create_task(self._write_stdin(proc.stdin, stdin_bytes))

        try:
            await asyncio.gather(stdout_task, stderr_task, stdin_task, proc.wait())
        except Exception:
            for task in (stdout_task, stderr_task, stdin_task):
                if task.done():
                    task.exception()
            raise

        return bytes(captured_output.stdout), bytes(captured_output.stderr)

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        *,
        stream_name: str,
        captured_output: _CapturedOutput,
        output_limit_event: asyncio.Event,
    ) -> None:
        if stream is None:
            return

        while True:
            chunk = await stream.read(READ_CHUNK_SIZE)
            if not chunk:
                return
            if captured_output.output_limit_stream is not None:
                continue

            buffer = captured_output.buffer_for(stream_name)
            if len(buffer) + len(chunk) > MAX_CAPTURE_BYTES_PER_STREAM:
                captured_output.mark_output_limit_exceeded(stream_name)
                output_limit_event.set()
                continue
            buffer.extend(chunk)

    async def _write_stdin(
        self,
        stdin: asyncio.StreamWriter | None,
        stdin_bytes: bytes | None,
    ) -> None:
        if stdin is None:
            return

        try:
            if stdin_bytes:
                stdin.write(stdin_bytes)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await stdin.wait_closed()

    def _send_signal(
        self,
        proc: asyncio.subprocess.Process,
        process_group_id: int,
        sig: signal.Signals,
    ) -> None:
        try:
            if process_group_id > 0:
                os.killpg(process_group_id, sig)
            elif proc.returncode is None:
                os.kill(proc.pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            return

    def _preserve_process_group_id(self, proc: asyncio.subprocess.Process) -> int:
        process_group_id = self._get_process_group_id(proc.pid)
        if process_group_id is not None:
            return process_group_id
        # start_new_session=True makes the spawned pid the process-group leader.
        return proc.pid

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
        completion_state: str | None = None,
        output_limit_stream: str | None = None,
    ) -> SandboxExecResult:
        exit_code, signal_name = self._decode_returncode(returncode)
        return SandboxExecResult(
            exit_code=exit_code,
            timed_out=timed_out,
            completion_state=completion_state,
            output_limit_stream=output_limit_stream,
            signal=signal_name,
            stdout=stdout or b"",
            stderr=stderr or b"",
            backend_name=spec.backend_name,
            profile_name=spec.profile_name,
            unsafe_fallback_used=spec.unsafe_fallback_used,
        )

    def _build_result_for_completion(
        self,
        spec: SandboxedLaunchSpec,
        returncode: int | None,
        stdout: bytes | None,
        stderr: bytes | None,
        *,
        captured_output: _CapturedOutput,
        timed_out: bool,
    ) -> SandboxExecResult:
        if captured_output.output_limit_stream is not None:
            return self._build_result(
                spec,
                returncode,
                b"",
                b"",
                timed_out=False,
                completion_state="output_limit_exceeded",
                output_limit_stream=captured_output.output_limit_stream,
            )
        return self._build_result(
            spec,
            returncode,
            stdout,
            stderr,
            timed_out=timed_out,
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
