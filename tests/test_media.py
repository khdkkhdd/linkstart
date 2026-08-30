import asyncio
import logging

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


def _run_result(returncode, stderr=b""):
    async def run(args, *, capture_stdout=False):
        return returncode, b"", stderr
    return run


async def test_remux_logs_the_decisive_stderr_line(monkeypatch, tmp_path, caplog):
    """A failed remux logs the decisive tail line, not just the first-200-chars
    version banner (2026-08-19: the real error was truncated away)."""
    media = MediaTools(ProcessRunner())
    banner = "ffmpeg version 8.1 Copyright\n" + "configuration: --enable-x " * 20
    stderr = (banner + "\n[mp4] moov atom not found\n").encode()
    monkeypatch.setattr(media.process, "run", _run_result(1, stderr))

    with caplog.at_level(logging.WARNING, logger="linkstart.downloader._media"):
        ok = await media.remux(tmp_path / "a.ts", tmp_path / "b.mp4")

    assert ok is False
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "moov atom not found" in msgs


async def test_remux_known_adts_contamination_logs_calm_info(
    monkeypatch, tmp_path, caplog
):
    """TwitCasting streams routinely carry non-ADTS junk in the audio PID; the
    copy remux then always fails on `aac_adtstoasc` and the tolerant re-encode
    is the expected, correct path. That routine detour must log one calm INFO
    line, not the three-warning ffmpeg dump reserved for unexpected failures."""
    media = MediaTools(ProcessRunner())
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) == 1:
            return 1, b"", b"[aac_adtstoasc] Error parsing ADTS frame header!\n"
        return 0, b"", b""

    monkeypatch.setattr(media.process, "run", run)
    durations = {"a.ts": 100, "b.mp4": 100}

    async def fake_probe(path):
        return durations.get(path.name)

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)

    with caplog.at_level(logging.INFO, logger="linkstart.downloader._media"):
        ok = await media.remux(tmp_path / "a.ts", tmp_path / "b.mp4")

    assert ok is True
    assert len(calls) == 2
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("non-ADTS" in m for m in infos)


async def test_remux_falls_back_to_tolerant_reencode(monkeypatch, tmp_path):
    """A src the lossless copy rejects is retried as a tolerant re-encode
    (video copied, audio re-encoded behind an explicit stereo mapping)."""
    media = MediaTools(ProcessRunner())
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        return (1, b"", b"boom") if len(calls) == 1 else (0, b"", b"")

    monkeypatch.setattr(media.process, "run", run)
    durations = {"a.ts": 100, "b.mp4": 99}

    async def fake_probe(path):
        return durations.get(path.name)

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(tmp_path / "a.ts", tmp_path / "b.mp4")

    assert ok is True
    assert len(calls) == 2
    fallback = calls[1]
    assert "-err_detect" in fallback and "ignore_err" in fallback
    assert "-c:v" in fallback and "copy" in fallback
    assert any("pan=stereo" in a for a in fallback)
    assert "-c:a" in fallback and "aac" in fallback


async def test_remux_fallback_rejects_truncated_output(monkeypatch, tmp_path):
    """A fallback exiting 0 with a much shorter output is data loss — it must
    report failure so the caller preserves the raw capture."""
    media = MediaTools(ProcessRunner())
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) > 1:
            (tmp_path / "b.mp4").write_bytes(b"stub")   # partial output
        return (1, b"", b"boom") if len(calls) == 1 else (0, b"", b"")

    monkeypatch.setattr(media.process, "run", run)
    durations = {"a.ts": 100, "b.mp4": 4}

    async def fake_probe(path):
        return durations.get(path.name)

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(tmp_path / "a.ts", tmp_path / "b.mp4")

    assert ok is False
    # The truncated partial output must not linger as a fake recording.
    assert not (tmp_path / "b.mp4").exists()


async def test_remux_fallback_accepted_when_source_unprobeable(monkeypatch, tmp_path):
    """Unprobeable source duration → nothing to compare; clean exit is success."""
    media = MediaTools(ProcessRunner())
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        return (1, b"", b"boom") if len(calls) == 1 else (0, b"", b"")

    monkeypatch.setattr(media.process, "run", run)

    async def fake_probe(path):
        return None

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    assert await media.remux(tmp_path / "a.ts", tmp_path / "b.mp4") is True
