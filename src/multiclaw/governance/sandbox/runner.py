from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass

from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import SandboxExecResult, SandboxedLaunchSpec

# Retained capture is capped per stream; transport buffering may briefly exceed this.
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


@dataclass
class _RunContext:
    proc: asyncio.subprocess.Process
    process_group_id: int
    captured_output: _CapturedOutput
    output_limit_event: asyncio.Event
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    stdin_task: asyncio.Task[None]
    proc_wait_task: asyncio.Task[int]
    all_done: asyncio.Future[tuple[None, None, None, int]]
    overflow_task: asyncio.Task[bool]

    @property
    def helper_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return (
            self.stdout_task,
            self.stderr_task,
            self.stdin_task,
            self.proc_wait_task,
        )


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

        proc = await self._spawn_process(spec)
        context = self._create_run_context(proc, spec.stdin_bytes)

        stop_reason = "completed"
        primary_exception: BaseException | None = None

        try:
            done, _ = await asyncio.wait(
                {context.all_done, context.overflow_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                stop_reason = "timed_out"
            elif context.overflow_task in done:
                stop_reason = "overflow"
            else:
                await context.all_done
                if context.captured_output.output_limit_stream is not None:
                    stop_reason = "overflow"
        except asyncio.CancelledError as exc:
            stop_reason = "cancelled"
            primary_exception = exc
        except Exception as exc:
            stop_reason = "failed"
            primary_exception = exc

        result = await self._finalize_run(
            context,
            spec,
            stop_reason=stop_reason,
        )

        if primary_exception is not None:
            raise primary_exception
        return result

    async def _spawn_process(self, spec: SandboxedLaunchSpec) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
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

    def _create_run_context(
        self,
        proc: asyncio.subprocess.Process,
        stdin_bytes: bytes | None,
    ) -> _RunContext:
        captured_output = _CapturedOutput(stdout=bytearray(), stderr=bytearray())
        output_limit_event = asyncio.Event()
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
        proc_wait_task = asyncio.create_task(proc.wait())
        return _RunContext(
            proc=proc,
            process_group_id=self._preserve_process_group_id(proc),
            captured_output=captured_output,
            output_limit_event=output_limit_event,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
            stdin_task=stdin_task,
            proc_wait_task=proc_wait_task,
            all_done=asyncio.gather(
                stdout_task,
                stderr_task,
                stdin_task,
                proc_wait_task,
            ),
            overflow_task=asyncio.create_task(output_limit_event.wait()),
        )

    async def _finalize_run(
        self,
        context: _RunContext,
        spec: SandboxedLaunchSpec,
        *,
        stop_reason: str,
    ) -> SandboxExecResult:
        if stop_reason != "completed":
            await self._terminate_process_group(context)

        await self._drain_run_context(context)

        return self._build_result_for_completion(
            spec,
            context.proc.returncode,
            bytes(context.captured_output.stdout),
            bytes(context.captured_output.stderr),
            captured_output=context.captured_output,
            timed_out=(stop_reason == "timed_out"),
        )

    async def _terminate_process_group(self, context: _RunContext) -> None:
        self._send_signal(context.proc, context.process_group_id, signal.SIGTERM)
        if await self._wait_for_process_group_exit(
            context.proc,
            context.process_group_id,
            timeout=self._term_grace_seconds,
            proc_wait_task=context.proc_wait_task,
        ):
            return

        self._send_signal(context.proc, context.process_group_id, signal.SIGKILL)
        await self._wait_for_process_group_exit(
            context.proc,
            context.process_group_id,
            timeout=self._term_grace_seconds,
            proc_wait_task=context.proc_wait_task,
        )

    async def _drain_run_context(self, context: _RunContext) -> None:
        if not context.overflow_task.done():
            context.overflow_task.cancel()

        for task in context.helper_tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*context.helper_tasks, return_exceptions=True)
        await asyncio.gather(
            context.all_done,
            context.overflow_task,
            return_exceptions=True,
        )

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
        return proc.pid

    def _process_group_exists(self, process_group_id: int) -> bool:
        if process_group_id <= 0:
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _get_process_group_id(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None
        except OSError:
            return None

    async def _wait_for_process_group_exit(
        self,
        proc: asyncio.subprocess.Process,
        process_group_id: int,
        *,
        timeout: float,
        proc_wait_task: asyncio.Task[int] | None = None,
    ) -> bool:
        owns_local_waiter = proc_wait_task is None
        local_proc_wait_task = proc_wait_task or asyncio.create_task(proc.wait())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        try:
            while True:
                if not self._process_group_exists(process_group_id):
                    await self._drain_wait_task(local_proc_wait_task, deadline=deadline)
                    return True

                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False

                if local_proc_wait_task.done():
                    await asyncio.sleep(min(0.01, remaining))
                    continue

                try:
                    await asyncio.wait_for(
                        asyncio.shield(local_proc_wait_task),
                        timeout=min(0.05, remaining),
                    )
                except asyncio.TimeoutError:
                    await asyncio.sleep(0)
                except Exception:
                    return not self._process_group_exists(process_group_id)
        finally:
            if owns_local_waiter:
                if not local_proc_wait_task.done():
                    local_proc_wait_task.cancel()
                await asyncio.gather(local_proc_wait_task, return_exceptions=True)

    async def _drain_wait_task(
        self,
        proc_wait_task: asyncio.Task[int],
        *,
        deadline: float,
    ) -> None:
        if proc_wait_task.done():
            await asyncio.gather(proc_wait_task, return_exceptions=True)
            return

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return

        await asyncio.gather(
            asyncio.wait_for(
                asyncio.shield(proc_wait_task),
                timeout=remaining,
            ),
            return_exceptions=True,
        )

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
