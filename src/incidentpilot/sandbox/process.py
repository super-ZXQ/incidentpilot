"""Bounded subprocesses with cancellation that waits for process-tree cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import suppress

OUTPUT_LIMIT = 256_000


def _windows_process(cmd, cwd, env, timeout, cancelled):
    # Psycopg requires a Selector loop on Windows; it has no asyncio subprocess
    # transport. This owned thread is always joined after cancellation.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancelled.is_set():
                    raise InterruptedError("process cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("subprocess timed out")
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > OUTPUT_LIMIT:
                    raise ValueError("subprocess output limit exceeded")
                time.sleep(0.03)
        finally:
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
                with suppress(ProcessLookupError):
                    process.kill()
            process.wait(timeout=10)
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read(OUTPUT_LIMIT).decode("utf-8", "replace"), stderr.read(OUTPUT_LIMIT).decode("utf-8", "replace")


async def run_process(cmd, *, cwd=None, env=None, timeout=120):
    if os.name == "nt":
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(_windows_process, cmd, cwd, env, timeout, cancelled))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            with suppress(Exception):
                await task
            raise

    process = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )

    async def drain(stream):
        chunks = bytearray()
        while chunk := await stream.read(8192):
            if len(chunks) + len(chunk) > OUTPUT_LIMIT:
                raise ValueError("subprocess output limit exceeded")
            chunks.extend(chunk)
        return chunks.decode("utf-8", "replace")

    readers = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(*readers)
            await process.wait()
        return process.returncode, stdout, stderr
    finally:
        # A parent may have exited while a child still owns stdout or is running.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
