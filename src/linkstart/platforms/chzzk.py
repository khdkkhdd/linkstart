"""Chzzk (Naver) platform — uses the live-detail JSON API."""
import asyncio
import logging

import aiohttp

from linkstart.auth import get_browser_cookies
from linkstart.models import ChannelConfig, DownloadProfile, LiveInfo
from linkstart.platforms._http import create_polling_session, polling_get
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


class ChzzkPlatform(Platform):
    name = "chzzk"

    LIVE_DETAIL_URL_TEMPLATE = (
        "https://api.chzzk.naver.com/service/v2/channels/{channel_id}/live-detail"
    )

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            # Chzzk's API rejects requests without a browser-like User-Agent.
            self._session = create_polling_session(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                }
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def check_live(self, channel: ChannelConfig) -> LiveInfo | None:
        url = self.LIVE_DETAIL_URL_TEMPLATE.format(channel_id=channel.channel_id)
        cookies = self.get_auth_cookies(channel)
        try:
            session = await self._get_session()
            async with polling_get(
                session,
                url,
                cookies=cookies or {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "chzzk: HTTP %s for %s", resp.status, channel.channel_id
                    )
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            log.warning("chzzk: request failed for %s: %s", channel.channel_id, e)
            return None
        except ValueError as e:
            log.warning("chzzk: invalid JSON for %s: %s", channel.channel_id, e)
            return None

        content = (data or {}).get("content") or {}
        if content.get("status") != "OPEN":
            return None
        live_id_raw = content.get("liveId")
        if live_id_raw is None or live_id_raw == "":
            return None
        return LiveInfo(
            live_id=str(live_id_raw),
            title=content.get("liveTitle") or "",
            url=self.build_url(channel, None),  # type: ignore[arg-type]
            thumbnail_url=content.get("liveImageUrl"),
        )

    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        return f"https://chzzk.naver.com/live/{channel.channel_id}"

    # Chzzk's live HLS is fMP4 (CMAF) whose media segments are named ".m4v", but
    # ffmpeg probes them as mov/mp4 — a demuxer that does not list "m4v" among its
    # extensions. With ffmpeg's default extension_picky=1 the hls demuxer rejects
    # the first segment ("mismatches allowed extensions in url …") and exits 183,
    # so every attempt produces zero bytes. Every chzzk format is protocol=m3u8
    # (never m3u8_native), so yt-dlp must go through ffmpeg — relaxing the check
    # on ffmpeg's input side is the only lever we have.
    FFMPEG_EXTENSION_CHECK_ARGS: tuple[str, ...] = (
        "--downloader-args", "ffmpeg_i:-extension_picky 0",
    )

    def _base_download_profile(self, channel: ChannelConfig) -> DownloadProfile:
        # HLS-live → mpegts container (.ts parts + --hls-use-mpegts).
        return DownloadProfile(
            container="mpegts",
            extra_args=self.FFMPEG_EXTENSION_CHECK_ARGS,
        )

    def get_auth_cookies(self, channel: ChannelConfig) -> dict[str, str] | None:
        if not channel.cookies_from_browser:
            return None
        cookies = get_browser_cookies(
            domain=".naver.com", browser=channel.cookies_from_browser
        )
        return cookies or None
