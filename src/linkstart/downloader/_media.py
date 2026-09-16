"""ffprobe/ffmpeg helpers: probe durations and remux part files."""
import asyncio
import logging
from pathlib import Path

from linkstart.downloader import _adts
from linkstart.downloader._process import (
    LOG_STDERR_LIMIT,
    ProcessRunner,
    _stderr_excerpt,
)

log = logging.getLogger(__name__)


class MediaTools:
    # Generous ceilings to bound pathological hangs; timeouts report failure, not crash.
    FFPROBE_TIMEOUT_SEC: float = 30.0
    FFMPEG_TIMEOUT_SEC: float = 600.0
    # Below this share of the source's duration, a fallback's exit-0 is truncation, not success.
    FALLBACK_MIN_DURATION_RATIO: float = 0.9
    FALLBACK_AUDIO_BITRATE: str = "160k"
    # aac_adtstoasc's complaint about TwitCasting's audio-PID junk → surgical clean.
    ADTS_CONTAMINATION_MARKER: bytes = b"Error parsing ADTS frame header"
    # Surgical cleaning reads the whole TS into memory; skip pathological sizes.
    SURGICAL_MAX_TS_BYTES: int = 1_500_000_000

    def __init__(self, process: ProcessRunner,
                 ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe") -> None:
        self.process = process
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin

    async def ffprobe_duration(self, path: Path) -> int | None:
        """Return integer seconds of `path` via ffprobe, or None on failure/timeout."""
        try:
            returncode, stdout, _ = await asyncio.wait_for(
                self.process.run(
                    [
                        self.ffprobe_bin,
                        "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "csv=p=0",
                        str(path),
                    ],
                    capture_stdout=True,
                ),
                timeout=self.FFPROBE_TIMEOUT_SEC,
            )
            if returncode != 0:
                return None
            text = stdout.decode(errors="replace").strip()
            if not text:
                return None
            return int(float(text.split(",")[0]))
        except asyncio.TimeoutError:
            log.warning(
                "ffprobe timed out after %.0fs for %s", self.FFPROBE_TIMEOUT_SEC, path
            )
            return None
        except Exception:
            log.exception("ffprobe failed for %s", path)
            return None

    async def ffmpeg_remux(self, video: Path, audio: Path, target: Path) -> bool:
        """Remux a video+audio part file pair into a playable mp4."""
        try:
            returncode, _, stderr = await asyncio.wait_for(
                self.process.run([
                    self.ffmpeg_bin, "-nostdin", "-y",
                    "-i", str(video), "-i", str(audio),
                    "-c", "copy", str(target),
                ]),
                timeout=self.FFMPEG_TIMEOUT_SEC,
            )
            if returncode != 0:
                log.warning(
                    "ffmpeg remux failed for %s/%s: %s",
                    video.name, audio.name,
                    stderr.decode(errors="replace")[:200],
                )
                return False
            return True
        except asyncio.TimeoutError:
            log.warning(
                "ffmpeg remux timed out after %.0fs for %s/%s",
                self.FFMPEG_TIMEOUT_SEC, video.name, audio.name,
            )
            return False
        except Exception:
            log.exception("ffmpeg remux raised")
            return False

    async def remux(self, src: Path, dst: Path) -> bool:
        """Rewrite `src` into the container implied by `dst`: lossless copy
        first, tolerant re-encode as fallback; a failed `dst` never lingers."""
        stderr = await self._remux_copy(src, dst)
        if stderr is None:
            return True
        contaminated = self.ADTS_CONTAMINATION_MARKER in stderr
        if contaminated:
            log.info(
                "copy remux rejected non-ADTS audio data in %s (known "
                "twitcasting contamination) — trying surgical clean",
                src.name,
            )
            if await self._remux_surgical(src, dst):
                log.info(
                    "surgical clean succeeded for %s → %s (lossless, gapless)",
                    src.name, dst.name,
                )
                return True
            log.warning(
                "surgical clean failed for %s — re-encoding audio, video copied",
                src.name,
            )
        else:
            if stderr:
                log.warning(
                    "ffmpeg remux failed: %s",
                    _stderr_excerpt(stderr, LOG_STDERR_LIMIT),
                )
            log.warning(
                "copy remux failed for %s — trying tolerant re-encode", src.name
            )
        if await self._remux_tolerant(src, dst):
            if not contaminated:
                log.warning(
                    "tolerant re-encode salvaged %s → %s", src.name, dst.name
                )
            return True
        dst.unlink(missing_ok=True)
        return False

    async def _remux_surgical(self, src: Path, dst: Path) -> bool:
        """Lossless contamination recovery: strip fillers, copy-remux original
        video + cleaned audio; rejected unless the output decodes error-free."""
        try:
            if src.stat().st_size > self.SURGICAL_MAX_TS_BYTES:
                log.info("surgical clean skipped for %s (too large)", src.name)
                return False
        except OSError:
            return False

        def _clean() -> bytes | None:
            ts = src.read_bytes()
            pid = _adts.find_audio_pid(ts)
            if pid is None:
                return None
            cleaned, stats = _adts.clean_adts(_adts.extract_audio_es(ts, pid))
            if not cleaned:
                return None
            log.info(
                "surgical clean of %s: kept %s frames (%.0fs), dropped %s "
                "fillers across %s boundaries",
                src.name, stats["frames_kept"], stats["duration_sec"],
                stats["frames_dropped"], stats["junk_regions"],
            )
            return cleaned

        try:
            cleaned = await asyncio.to_thread(_clean)
        except Exception:
            log.exception("surgical clean raised for %s", src)
            return False
        if cleaned is None:
            return False

        tmp = dst.with_name(dst.stem + ".cleanaudio.aac")
        try:
            tmp.write_bytes(cleaned)
            returncode, _, stderr = await asyncio.wait_for(
                self.process.run([
                    self.ffmpeg_bin, "-nostdin", "-y",
                    "-i", str(src), "-i", str(tmp),
                    "-map", "0:v?", "-map", "1:a",
                    "-c", "copy", str(dst),
                ]),
                timeout=self.FFMPEG_TIMEOUT_SEC,
            )
            if returncode != 0:
                log.warning(
                    "surgical remux failed: %s",
                    _stderr_excerpt(stderr, LOG_STDERR_LIMIT),
                )
                return False

            # Audio-only decode check — the untouched video slate has dts quirks.
            returncode, _, stderr = await asyncio.wait_for(
                self.process.run([
                    self.ffmpeg_bin, "-nostdin", "-v", "error",
                    "-i", str(dst), "-map", "0:a", "-f", "null", "-",
                ]),
                timeout=self.FFMPEG_TIMEOUT_SEC,
            )
            if returncode != 0 or stderr.strip():
                log.warning(
                    "surgical output fails decode verification: %s",
                    _stderr_excerpt(stderr, LOG_STDERR_LIMIT),
                )
                return False

            src_duration = await self.ffprobe_duration(src)
            if src_duration is not None:
                dst_duration = await self.ffprobe_duration(dst)
                if (
                    dst_duration is None
                    or dst_duration < src_duration * self.FALLBACK_MIN_DURATION_RATIO
                ):
                    log.warning(
                        "surgical output truncated for %s (%ss of %ss)",
                        src.name, dst_duration, src_duration,
                    )
                    return False
            return True
        except asyncio.TimeoutError:
            log.error("surgical remux timed out for %s", src)
            return False
        finally:
            tmp.unlink(missing_ok=True)

    async def _remux_copy(self, src: Path, dst: Path) -> bytes | None:
        """Lossless container rewrite (copy codec). Returns None on success,
        the ffmpeg stderr on failure (empty on timeout, which logs here)."""
        try:
            returncode, _, stderr = await asyncio.wait_for(
                self.process.run([
                    self.ffmpeg_bin, "-y", "-i", str(src), "-c", "copy", str(dst),
                ]),
                timeout=self.FFMPEG_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.error(
                "ffmpeg remux timed out after %.0fs for %s", self.FFMPEG_TIMEOUT_SEC, src
            )
            return b""
        if returncode != 0:
            return stderr
        return None

    async def _remux_tolerant(self, src: Path, dst: Path) -> bool:
        """Salvage re-encode: copy video, re-encode audio, ignore decode errors.
        The explicit stereo `pan` keeps a corrupt frame's bogus channel layout
        from wedging the filter graph mid-file."""
        try:
            returncode, _, stderr = await asyncio.wait_for(
                self.process.run([
                    self.ffmpeg_bin, "-y",
                    "-err_detect", "ignore_err",
                    "-i", str(src),
                    "-c:v", "copy",
                    "-af", "pan=stereo|c0=c0|c1=c1,aresample=async=1",
                    "-c:a", "aac", "-b:a", self.FALLBACK_AUDIO_BITRATE,
                    str(dst),
                ]),
                timeout=self.FFMPEG_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.error(
                "tolerant re-encode timed out after %.0fs for %s",
                self.FFMPEG_TIMEOUT_SEC, src,
            )
            return False
        if returncode != 0:
            log.error(
                "tolerant re-encode failed for %s: %s",
                src.name, _stderr_excerpt(stderr, LOG_STDERR_LIMIT),
            )
            return False

        # Exit-0 covering only part of the source is data loss, not success.
        src_duration = await self.ffprobe_duration(src)
        if src_duration is None:
            return True
        dst_duration = await self.ffprobe_duration(dst)
        if (
            dst_duration is None
            or dst_duration < src_duration * self.FALLBACK_MIN_DURATION_RATIO
        ):
            log.error(
                "tolerant re-encode produced truncated output for %s (%ss of %ss)",
                src.name, dst_duration, src_duration,
            )
            return False
        return True
