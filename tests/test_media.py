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


async def test_remux_contamination_unsalvageable_source_reencodes(
    monkeypatch, tmp_path, caplog
):
    """No recoverable ADTS stream → tolerant re-encode; contamination stays calm INFO."""
    from linkstart.downloader import _adts
    media = MediaTools(ProcessRunner())
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata")
    monkeypatch.setattr(_adts, "find_audio_pid", lambda ts: None)
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
        ok = await media.remux(src, dst)

    assert ok is True
    assert any("pan=stereo" in a for a in calls[-1])
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


# ---------- surgical clean integration ----------

def _patch_surgical_inputs(monkeypatch, es=b"ES", cleaned=b"CLEANED"):
    from linkstart.downloader import _adts
    monkeypatch.setattr(_adts, "find_audio_pid", lambda ts: 257)
    monkeypatch.setattr(_adts, "extract_audio_es", lambda ts, pid: es)
    monkeypatch.setattr(
        _adts, "clean_adts",
        lambda e: (cleaned, {"frames_kept": 9, "frames_dropped": 3,
                             "junk_regions": 1, "duration_sec": 100.0}),
    )


async def test_remux_contamination_uses_surgical_clean_first(
    monkeypatch, tmp_path, caplog
):
    """Contamination → surgical lossless clean; the hole-punching re-encode must NOT run."""
    media = MediaTools(ProcessRunner())
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata")
    _patch_surgical_inputs(monkeypatch)
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) == 1:   # copy remux fails on contamination
            return 1, b"", b"[aac_adtstoasc] Error parsing ADTS frame header!\n"
        if len(calls) == 2:   # surgical remux (src video + clean aac)
            dst.write_bytes(b"out")
            return 0, b"", b""
        return 0, b"", b""    # decode-verify: clean stderr

    monkeypatch.setattr(media.process, "run", run)
    durations = {"a.ts": 100, "b.mp4": 97}

    async def fake_probe(path):
        return durations.get(path.name)

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)

    with caplog.at_level(logging.INFO, logger="linkstart.downloader._media"):
        ok = await media.remux(src, dst)

    assert ok is True
    surgical = calls[1]
    assert any(str(src) in a for a in surgical)
    assert any(a.endswith(".aac") for a in map(str, surgical))
    assert "-c" in surgical and "copy" in surgical
    # Verify decodes audio only — the untouched video slate has dts quirks.
    verify = calls[2]
    assert "0:a" in verify and "0:v" not in " ".join(verify)
    # No re-encode anywhere: three calls total, none with an aac encoder.
    assert len(calls) == 3
    assert not any("-c:a" in c for c in calls)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    # The clean-audio temp file must not linger.
    assert list(tmp_path.glob("*.aac")) == []


async def test_remux_surgical_failure_falls_back_to_tolerant(
    monkeypatch, tmp_path
):
    media = MediaTools(ProcessRunner())
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata")
    _patch_surgical_inputs(monkeypatch)
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) == 1:   # copy remux: contamination
            return 1, b"", b"[aac_adtstoasc] Error parsing ADTS frame header!\n"
        if len(calls) == 2:   # surgical remux fails
            return 1, b"", b"boom"
        dst.write_bytes(b"reencoded")
        return 0, b"", b""    # tolerant re-encode succeeds

    monkeypatch.setattr(media.process, "run", run)
    durations = {"a.ts": 100, "b.mp4": 98}

    async def fake_probe(path):
        return durations.get(path.name)

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(src, dst)

    assert ok is True
    assert any("pan=stereo" in a for a in calls[-1])   # tolerant path ran


async def test_remux_surgical_rejects_dirty_decode(monkeypatch, tmp_path):
    """Surgical output that still trips the decoder is rejected → fallback."""
    media = MediaTools(ProcessRunner())
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata")
    _patch_surgical_inputs(monkeypatch)
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) == 1:
            return 1, b"", b"[aac_adtstoasc] Error parsing ADTS frame header!\n"
        if len(calls) == 2:
            dst.write_bytes(b"out")
            return 0, b"", b""
        if len(calls) == 3:   # decode-verify reports decoder errors
            return 0, b"", b"[aac] channel element 1.13 is not allocated\n"
        dst.write_bytes(b"reencoded")
        return 0, b"", b""    # tolerant fallback

    monkeypatch.setattr(media.process, "run", run)
    durations = {"a.ts": 100, "b.mp4": 97}

    async def fake_probe(path):
        return durations.get(path.name)

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(src, dst)

    assert ok is True
    assert any("pan=stereo" in a for a in calls[-1])


async def test_remux_surgical_rejects_truncated_duration(monkeypatch, tmp_path):
    media = MediaTools(ProcessRunner())
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata")
    _patch_surgical_inputs(monkeypatch)
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) == 1:
            return 1, b"", b"[aac_adtstoasc] Error parsing ADTS frame header!\n"
        if len(calls) == 2:
            dst.write_bytes(b"out")
            return 0, b"", b""
        if len(calls) == 3:
            return 0, b"", b""
        dst.write_bytes(b"reencoded")
        return 0, b"", b""

    monkeypatch.setattr(media.process, "run", run)

    # Surgical dst probes half-length (rejected); tolerant's dst full-length.
    async def fake_probe(path):
        if path.name == "a.ts":
            return 100
        return 98 if len(calls) >= 4 else 50

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(src, dst)

    assert ok is True
    assert any("pan=stereo" in a for a in calls[-1])


async def test_remux_no_contamination_skips_surgical(monkeypatch, tmp_path):
    """No ADTS marker → straight to re-encode; surgery is contamination-specific."""
    media = MediaTools(ProcessRunner())
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata")
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        return (1, b"", b"moov atom not found") if len(calls) == 1 else (0, b"", b"")

    monkeypatch.setattr(media.process, "run", run)

    async def fake_probe(path):
        return 100 if path.name == "a.ts" else 99

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(src, dst)

    assert ok is True
    assert len(calls) == 2
    assert any("pan=stereo" in a for a in calls[1])


async def test_remux_surgical_skips_oversized_source(monkeypatch, tmp_path):
    media = MediaTools(ProcessRunner())
    media.SURGICAL_MAX_TS_BYTES = 3
    src, dst = tmp_path / "a.ts", tmp_path / "b.mp4"
    src.write_bytes(b"tsdata-larger-than-cap")
    calls = []

    async def run(args, *, capture_stdout=False):
        calls.append(args)
        if len(calls) == 1:
            return 1, b"", b"[aac_adtstoasc] Error parsing ADTS frame header!\n"
        dst.write_bytes(b"reencoded")
        return 0, b"", b""

    monkeypatch.setattr(media.process, "run", run)

    async def fake_probe(path):
        return 100 if path.name == "a.ts" else 99

    monkeypatch.setattr(media, "ffprobe_duration", fake_probe)
    ok = await media.remux(src, dst)

    assert ok is True
    assert len(calls) == 2                      # copy fail → tolerant only
    assert any("pan=stereo" in a for a in calls[1])
