import asyncio

from linkstart.downloader._process import ProcessRunner


async def test_run_captures_stdout_and_returncode():
    runner = ProcessRunner()
    rc, out, err = await runner.run(
        ["python", "-c", "import sys; print('hi'); sys.exit(0)"],
        capture_stdout=True,
    )
    assert rc == 0
    assert out.strip() == b"hi"


async def test_run_returns_stderr_on_failure():
    runner = ProcessRunner()
    rc, _out, err = await runner.run(
        ["python", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    assert rc == 3
    assert b"boom" in err


async def test_spawn_then_drain_or_kill_terminates():
    runner = ProcessRunner()
    proc = await runner.spawn(["python", "-c", "import time; time.sleep(30)"])
    comm = asyncio.create_task(proc.communicate())
    stderr = await runner.drain_or_kill(proc, comm)
    assert proc.returncode is not None  # was reaped
    assert isinstance(stderr, bytes)
