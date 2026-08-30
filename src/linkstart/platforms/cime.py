"""CI.ME platform — live detection via the live-page API
(``/api/app/page/live/{slug}``, unauthenticated); the same response carries
the IVS HLS playback URL."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import aiohttp

from linkstart.auth import get_browser_cookies
from linkstart.models import ChannelConfig, DownloadProfile, LiveInfo
from linkstart.platforms._http import create_polling_session, polling_get
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CimePlatform(Platform):
    name = "cime"

    PAGE_URL_TEMPLATE = "https://ci.me/@{channel_id}/live"
    API_URL_TEMPLATE = "https://ci.me/api/app/page/live/{channel_id}"

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        # channel slug -> (live id, HLS master URL)
        self._playback_by_channel: dict[str, tuple[str, str]] = {}
        # channel slug -> (live id, from-start VOD master URL)
        self._full_by_channel: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _slug(channel: ChannelConfig) -> str:
        # Be forgiving when a user copies the visible "@handle" form.
        return channel.channel_id.removeprefix("@")

    def _page_url(self, channel: ChannelConfig) -> str:
        return self.PAGE_URL_TEMPLATE.format(channel_id=self._slug(channel))

    def _api_url(self, channel: ChannelConfig) -> str:
        return self.API_URL_TEMPLATE.format(channel_id=self._slug(channel))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = create_polling_session(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                }
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def check_live(self, channel: ChannelConfig) -> LiveInfo | None:
        cookies = self.get_auth_cookies(channel)
        try:
            session = await self._get_session()
            async with polling_get(
                session,
                self._api_url(channel),
                cookies=cookies or {},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    log.warning(
                        "cime: HTTP %s for %s", resp.status, channel.channel_id
                    )
                    return None
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            log.warning("cime: request failed for %s: %s", channel.channel_id, e)
            return None

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("cime: invalid JSON for %s: %s", channel.channel_id, e)
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        live = data.get("live") if isinstance(data, dict) else None
        if not isinstance(live, dict):
            return None
        if live.get("state") != "ACTIVE":
            return None

        opened_raw = live.get("openedAt")
        if isinstance(opened_raw, str) and opened_raw:
            live_id = opened_raw
        else:
            live_id = str(live.get("id"))

        slug = self._slug(channel)
        playback = live.get("playback")
        playback_url = playback.get("url") if isinstance(playback, dict) else None
        if isinstance(playback_url, str) and playback_url:
            self._playback_by_channel[slug] = (live_id, playback_url)
        else:
            self._playback_by_channel.pop(slug, None)
            log.warning(
                "cime: live detected for %s but no playback URL in API response",
                channel.channel_id,
            )

        thumbnail = live.get("imageUrl")
        if isinstance(thumbnail, str) and "/media/" in thumbnail:
            session_prefix = thumbnail.split("/media/", 1)[0]
            self._full_by_channel[slug] = (
                live_id, f"{session_prefix}/media/hls/master.m3u8"
            )
        else:
            self._full_by_channel.pop(slug, None)

        return LiveInfo(
            live_id=live_id,
            title=live.get("title") if isinstance(live.get("title"), str) else "",
            url=self._page_url(channel),
            started_at=_parse_datetime(opened_raw),
            thumbnail_url=thumbnail if isinstance(thumbnail, str) and thumbnail else None,
        )

    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        cached = self._playback_by_channel.get(self._slug(channel))
        if cached is not None and cached[0] == live.live_id:
            return cached[1]
        # yt-dlp's generic extractor reads the page's JSON-LD — safe fallback.
        return self._page_url(channel)

    def build_full_url(self, channel: ChannelConfig, live: LiveInfo) -> str | None:
        cached = self._full_by_channel.get(self._slug(channel))
        if cached is not None and cached[0] == live.live_id:
            return cached[1]
        return None

    def recording_strategy(self, ctx):
        from linkstart.downloader._snapshot_dual import SnapshotDualRecordingStrategy
        return SnapshotDualRecordingStrategy(ctx)

    def _base_download_profile(self, channel: ChannelConfig) -> DownloadProfile:
        # IVS HLS: native downloader + mpegts keeps interrupted captures playable.
        return DownloadProfile(
            container="mpegts",
            downloader="native",
            extra_args=("--referer", self._page_url(channel)),
        )

    def get_auth_cookies(self, channel: ChannelConfig) -> dict[str, str] | None:
        if not channel.cookies_from_browser:
            return None
        cookies = get_browser_cookies(
            domain=".ci.me", browser=channel.cookies_from_browser
        )
        return cookies or None
