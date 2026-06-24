import asyncio

from linkstart.downloader._media import MediaTools
from linkstart.downloader._process import ProcessRunner


async def test_ffprobe_duration_none_on_bad_file(tmp_path):
    media = MediaTools(ProcessRunner())
    bad = tmp_path / "nope.mp4"
    bad.write_bytes(b"not media")
    assert await media.ffprobe_duration(bad) is None


async def test_remux_infers_container_from_dst_suffix(monkeypatch, tmp_path):
    media = MediaTools(ProcessRunner())
    calls = {}

    async def fake_run(args, *, capture_stdout=False):
        calls["args"] = args
        return 0, b"", b""

    monkeypatch.setattr(media.process, "run", fake_run)
    src = tmp_path / "part01.ts"
    dst = tmp_path / "out.mp4"
    src.write_bytes(b"x")
    ok = await media.remux(src, dst)
    assert ok is True
    assert str(src) in calls["args"] and str(dst) in calls["args"]
    assert "-c" in calls["args"] and "copy" in calls["args"]


async def _hang(*a, **k):
    await asyncio.sleep(30)


async def test_ffprobe_duration_returns_none_on_timeout(monkeypatch, tmp_path):
    """A wedged ffprobe (corrupt file / slow FS) must time out and return None,
    not hang the worker forever during cleanup."""
    media = MediaTools(ProcessRunner())
    media.FFPROBE_TIMEOUT_SEC = 0.01
    monkeypatch.setattr(media.process, "run", _hang)
    # Outer guard: if the internal timeout is missing this raises TimeoutError.
    result = await asyncio.wait_for(
        media.ffprobe_duration(tmp_path / "x.mp4"), timeout=2
    )
    assert result is None


async def test_remux_returns_false_on_timeout(monkeypatch, tmp_path):
    media = MediaTools(ProcessRunner())
    media.FFMPEG_TIMEOUT_SEC = 0.01
    monkeypatch.setattr(media.process, "run", _hang)
    result = await asyncio.wait_for(
        media.remux(tmp_path / "a.ts", tmp_path / "b.mp4"), timeout=2
    )
    assert result is False


async def test_ffmpeg_remux_returns_false_on_timeout(monkeypatch, tmp_path):
    media = MediaTools(ProcessRunner())
    media.FFMPEG_TIMEOUT_SEC = 0.01
    monkeypatch.setattr(media.process, "run", _hang)
    result = await asyncio.wait_for(
        media.ffmpeg_remux(
            tmp_path / "v.mp4", tmp_path / "a.mp4", tmp_path / "o.mp4"
        ),
        timeout=2,
    )
    assert result is False
