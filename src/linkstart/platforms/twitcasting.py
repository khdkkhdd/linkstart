"""TwitCasting platform — uses the streamserver.php JSON endpoint."""
import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from linkstart.models import ChannelConfig, LiveInfo, ValidationResult
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


class TwitcastingPlatform(Platform):
    name = "twitcasting"

    STREAMSERVER_URL = "https://twitcasting.tv/streamserver.php"

    # Login-wall detection thresholds. The login-wall placeholder TwitCasting
    # serves to unauthenticated viewers is ~52 kbps; real low-quality streams
    # sit well above 80 kbps. Duration floor avoids flagging genuinely-short
    # broadcasts that happen to be low-bitrate.
    LOGIN_WALL_MAX_BITRATE_KBPS: float = 80.0
    LOGIN_WALL_MIN_DURATION_S: float = 60.0

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        ffprobe_bin: str = "ffprobe",
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._ffprobe_bin = ffprobe_bin

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def check_live(self, channel: ChannelConfig) -> LiveInfo | None:
        params = {"target": channel.channel_id, "mode": "client"}
        try:
            session = await self._get_session()
            async with session.get(
                self.STREAMSERVER_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "twitcasting: HTTP %s for %s", resp.status, channel.channel_id
                    )
                    return None
                data = await resp.json(content_type=None)
        # ConnectionError is kept defensively: aiohttp normally wraps socket
        # errors in ClientConnectionError (a ClientError subclass), but some
        # transports / test doubles may surface a bare builtin ConnectionError.
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            log.warning(
                "twitcasting: request failed for %s: %s", channel.channel_id, e
            )
            return None
        except ValueError as e:
            log.warning("twitcasting: invalid JSON for %s: %s", channel.channel_id, e)
            return None

        movie = (data or {}).get("movie") or {}
        if not movie.get("live"):
            return None
        live_id_raw = movie.get("id")
        if live_id_raw is None or live_id_raw == "":
            return None
        live_id = str(live_id_raw)
        title = movie.get("title") or ""
        return LiveInfo(
            live_id=live_id,
            title=title,
            url=f"https://twitcasting.tv/{channel.channel_id}",
        )

    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        return f"https://twitcasting.tv/{channel.channel_id}"

    def yt_dlp_args(self, channel: ChannelConfig) -> list[str]:
        # --no-live-from-start is yt-dlp's default; no need to spell it out.
        # The Downloader does not pass --live-from-start for TwitCasting
        # because supports_live_from_start is False (inherited from base).
        return ["--hls-use-mpegts"]

    async def validate_recording(self, file_path: Path) -> ValidationResult:
        metrics = await self._ffprobe_metrics(file_path)
        if metrics is None:
            return ValidationResult(status="ok")
        duration_s = metrics["duration_s"]
        bitrate_kbps = metrics["bitrate_kbps"]
        if (
            duration_s > self.LOGIN_WALL_MIN_DURATION_S
            and bitrate_kbps < self.LOGIN_WALL_MAX_BITRATE_KBPS
        ):
            return ValidationResult(
                status="invalid",
                reason=(
                    f"likely login wall: {bitrate_kbps:.0f} kbps over "
                    f"{duration_s:.0f}s"
                ),
                metrics=dict(metrics),
            )
        return ValidationResult(status="ok", metrics=dict(metrics))

    async def _ffprobe_metrics(self, file_path: Path) -> dict[str, float] | None:
        """Probe duration and bit_rate via ffprobe -of json. Returns
        {"duration_s", "bitrate_kbps"} or None on any failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration,bit_rate",
                "-of", "json",
                str(file_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            data = json.loads(stdout.decode(errors="replace"))
            fmt = data.get("format") or {}
            duration_s = float(fmt["duration"])
            bitrate_bps = float(fmt["bit_rate"])
            return {"duration_s": duration_s, "bitrate_kbps": bitrate_bps / 1000}
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None
