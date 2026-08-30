"""ffprobe/ffmpeg helpers: probe durations and remux part files."""
import asyncio
import logging
from pathlib import Path

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
    # TwitCasting interleaves non-ADTS junk into the audio PID of some stream
    # modes; aac_adtstoasc then always rejects the copy and the tolerant
    # re-encode is the expected, correct path — not an anomaly worth warning.
    ADTS_CONTAMINATION_MARKER: bytes = b"Error parsing ADTS frame header"

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
                "twitcasting contamination) — re-encoding audio, video copied",
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
