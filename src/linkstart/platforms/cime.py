"""CI.ME platform — detects live broadcasts from the public live page.

CI.ME publishes a schema.org ``VideoObject`` for active broadcasts.  Using
that server-rendered JSON-LD keeps polling unauthenticated and avoids spawning
yt-dlp merely to determine whether a channel is live.  The same object exposes
the IVS HLS master URL, which is cached for the recorder.
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

import aiohttp

from linkstart.auth import get_browser_cookies
from linkstart.models import ChannelConfig, DownloadProfile, LiveInfo
from linkstart.platforms.base import Platform

log = logging.getLogger(__name__)


class _JsonLdParser(HTMLParser):
    """Collect application/ld+json script bodies without extra dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "script":
            return
        attr_map = {key.lower(): value for key, value in attrs}
        script_type = (attr_map.get("type") or "").lower()
        if script_type == "application/ld+json":
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._current is not None:
            self.documents.append("".join(self._current))
            self._current = None


def _has_type(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    return isinstance(value, list) and expected in value


def _find_live_video(document: Any) -> dict[str, Any] | None:
    """Return the live VideoObject from one decoded JSON-LD document."""
    if isinstance(document, list):
        candidates = document
    elif isinstance(document, dict):
        graph = document.get("@graph")
        candidates = graph if isinstance(graph, list) else [document]
    else:
        return None

    for item in candidates:
        if not isinstance(item, dict) or not _has_type(item.get("@type"), "VideoObject"):
            continue
        publication = item.get("publication")
        if not isinstance(publication, dict):
            continue
        if not _has_type(publication.get("@type"), "BroadcastEvent"):
            continue
        if publication.get("isLiveBroadcast") is True:
            return item
    return None


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

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        # channel slug -> (live id, HLS master URL)
        self._playback_by_channel: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _slug(channel: ChannelConfig) -> str:
        # Be forgiving when a user copies the visible "@handle" form.
        return channel.channel_id.removeprefix("@")

    def _page_url(self, channel: ChannelConfig) -> str:
        return self.PAGE_URL_TEMPLATE.format(channel_id=self._slug(channel))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
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
        page_url = self._page_url(channel)
        cookies = self.get_auth_cookies(channel)
        try:
            session = await self._get_session()
            async with session.get(
                page_url,
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
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            log.warning("cime: request failed for %s: %s", channel.channel_id, e)
            return None

        parser = _JsonLdParser()
        try:
            parser.feed(html)
        except Exception as e:
            log.warning("cime: invalid HTML for %s: %s", channel.channel_id, e)
            return None

        video: dict[str, Any] | None = None
        for raw_document in parser.documents:
            try:
                document = json.loads(raw_document)
            except (json.JSONDecodeError, TypeError):
                continue
            video = _find_live_video(document)
            if video is not None:
                break

        playback_url = video.get("contentUrl") if video is not None else None
        if video is None or not isinstance(playback_url, str) or not playback_url:
            self._playback_by_channel.pop(self._slug(channel), None)
            return None

        publication = video.get("publication")
        assert isinstance(publication, dict)  # guaranteed by _find_live_video
        start_raw = publication.get("startDate") or video.get("uploadDate")
        started_at = _parse_datetime(start_raw)
        if isinstance(start_raw, str) and start_raw:
            live_id = start_raw
        else:
            # Defensive fallback for an incomplete JSON-LD document. The
            # playback URL is stable for the lifetime of the active broadcast.
            live_id = hashlib.sha256(playback_url.encode()).hexdigest()[:20]

        thumbnail_raw = video.get("thumbnailUrl")
        if isinstance(thumbnail_raw, list):
            thumbnail = next(
                (value for value in thumbnail_raw if isinstance(value, str) and value),
                None,
            )
        elif isinstance(thumbnail_raw, str) and thumbnail_raw:
            thumbnail = thumbnail_raw
        else:
            thumbnail = None

        slug = self._slug(channel)
        self._playback_by_channel[slug] = (live_id, playback_url)
        public_url = video.get("url")
        if not isinstance(public_url, str) or not public_url:
            public_url = page_url
        return LiveInfo(
            live_id=live_id,
            title=video.get("name") if isinstance(video.get("name"), str) else "",
            url=public_url,
            started_at=started_at,
            thumbnail_url=thumbnail,
        )

    def build_url(self, channel: ChannelConfig, live: LiveInfo) -> str:
        cached = self._playback_by_channel.get(self._slug(channel))
        if cached is not None and cached[0] == live.live_id:
            return cached[1]
        # The generic yt-dlp extractor understands CI.ME's JSON-LD, so the
        # public page remains a safe fallback if the in-memory cache is absent.
        return self._page_url(channel)

    def _base_download_profile(self, channel: ChannelConfig) -> DownloadProfile:
        # CI.ME uses Amazon IVS HLS. Native fragment downloading plus mpegts
        # part files keeps an interrupted capture playable and restartable.
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
