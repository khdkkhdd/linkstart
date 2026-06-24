"""ffprobe/ffmpeg helpers: probe durations and remux part files."""
import asyncio
import logging
from pathlib import Path

from linkstart.downloader._process import ProcessRunner

log = logging.getLogger(__name__)


class MediaTools:
    # Generous ceilings to bound pathological hangs; timeouts report failure, not crash.
    FFPROBE_TIMEOUT_SEC: float = 30.0
    FFMPEG_TIMEOUT_SEC: float = 600.0

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
        """Rewrite `src` into the container implied by `dst` (copy codec)."""
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
            return False
        if returncode != 0:
            log.error("ffmpeg remux failed: %s", stderr.decode(errors="replace")[:200])
            return False
        return True
