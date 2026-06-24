"""Subprocess spawn + teardown for the recorder — the single place a child
process is created and reaped, so process-group teardown lives in one spot."""
import asyncio
import contextlib
import os
import signal


class ProcessRunner:
    # Max wait after each teardown signal (SIGTERM, then SIGKILL).
    TEARDOWN_TERM_WAIT_SEC: float = 10.0

    async def spawn(
        self, args: list[str], *, capture_stdout: bool = False
    ) -> asyncio.subprocess.Process:
        """Create a child in its own session/process-group, stderr captured."""
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE if capture_stdout
            else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def run(
        self, args: list[str], *, capture_stdout: bool = False
    ) -> tuple[int | None, bytes, bytes]:
        """Run to completion, always capturing stderr. Returns (rc, stdout, stderr).
        Terminate the child on unwind so it can't survive as an orphan."""
        proc = await self.spawn(args, capture_stdout=capture_stdout)
        try:
            stdout, stderr = await proc.communicate()
            return proc.returncode, stdout or b"", stderr or b""
        finally:
            await self.terminate_and_reap(proc)

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Signal the child's whole process group (so the ffmpeg grandchild dies too)."""
        try:
            os.killpg(proc.pid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError, TypeError, AttributeError):
            pass
        fallback = proc.terminate if sig == signal.SIGTERM else proc.kill
        with contextlib.suppress(ProcessLookupError):
            fallback()

    async def drain_or_kill(
        self, proc: asyncio.subprocess.Process, comm_task: asyncio.Task
    ) -> bytes:
        """Stop a running child and return whatever stderr it flushed.
        Escalates SIGTERM → SIGKILL; returns empty bytes if the child never yields EOF."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            self._signal_group(proc, sig)
            try:
                _, stderr = await asyncio.wait_for(
                    asyncio.shield(comm_task), timeout=self.TEARDOWN_TERM_WAIT_SEC
                )
                return stderr or b""
            except asyncio.TimeoutError:
                continue
        return b""

    async def terminate_and_reap(self, proc: asyncio.subprocess.Process) -> None:
        """Best-effort orphan cleanup: SIGTERM the child's group, escalate to SIGKILL on failure."""
        if proc.returncode is not None:
            return
        self._signal_group(proc, signal.SIGTERM)
        try:
            await proc.wait()
        except BaseException:
            self._signal_group(proc, signal.SIGKILL)
