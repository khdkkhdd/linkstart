"""Discord webhook notifier."""
import asyncio
import logging

import aiohttp

from linkstart.models import Event, EventType
from linkstart.notifier.base import Notifier

log = logging.getLogger(__name__)

COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_GREEN = 0x2ECC71
COLOR_GRAY = 0x95A5A6
COLOR_BLUE = 0x3498DB


def _format_size(b: int | None) -> str:
    if not b:
        return "0 B"
    val = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} PB"


def _format_duration(sec: int | None) -> str:
    if not sec:
        return "0s"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def build_embed(event: Event) -> dict:
    """Convert an Event into a Discord embed dict."""
    ch = event.channel
    if event.type == EventType.LIVE_STARTED:
        embed: dict = {
            "title": f"🔴 [{ch.platform}] {ch.display_name} is live",
            "description": event.live.title if event.live else "",
            "color": COLOR_RED,
        }
        if event.live and event.live.thumbnail_url:
            embed["thumbnail"] = {"url": event.live.thumbnail_url}
        return embed

    if event.type == EventType.DOWNLOAD_STARTED:
        return {
            "title": "📥 Recording started",
            "description": f"[{ch.platform}] {ch.display_name}",
            "color": COLOR_YELLOW,
        }

    if event.type == EventType.DOWNLOAD_INTERRUPTED:
        return {
            "title": "⚠️ Recording interrupted (auto-retrying)",
            "description": f"[{ch.platform}] {ch.display_name}",
            "color": COLOR_YELLOW,
            "fields": [
                {"name": "detail", "value": event.message or "", "inline": False},
            ],
        }

    if event.type == EventType.DOWNLOAD_FINISHED:
        fields = [
            {"name": "file", "value": str(event.file_path), "inline": False},
            {"name": "size", "value": _format_size(event.size_bytes), "inline": True},
            {"name": "duration", "value": _format_duration(event.duration_sec), "inline": True},
            {"name": "retries", "value": str(event.retry_count), "inline": True},
        ]
        if event.extra_files:
            fields.append({
                "name": "extra files",
                "value": f"{len(event.extra_files)} (edge tails / recovered)",
                "inline": True,
            })
        return {
            "title": "✅ Recording finished",
            "color": COLOR_GREEN,
            "fields": fields,
        }

    if event.type == EventType.ERROR:
        embed = {
            "title": "❌ Error",
            "description": event.message or "(no detail)",
            "color": COLOR_GRAY,
        }
        if event.file_path is not None:
            embed["fields"] = [
                {"name": "file", "value": str(event.file_path), "inline": False},
            ]
        return embed

    if event.type == EventType.SUMMARY:
        return {
            "title": "📊 Daily summary",
            "description": event.message or "",
            "color": COLOR_BLUE,
        }

    raise ValueError(f"Unknown event type: {event.type}")


class DiscordNotifier(Notifier):
    def __init__(
        self,
        webhook_url: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        # secret: do not log or repr; bearer-equivalent credential
        self._webhook_url = webhook_url
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def notify(self, event: Event) -> None:
        try:
            session = await self._get_session()
            async with session.post(
                self._webhook_url,
                json={"embeds": [build_embed(event)]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    log.warning("discord webhook returned %s", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("discord webhook send failed: %s", e)
        except Exception:
            # Notifications must never raise into the caller — recording must continue.
            log.exception("discord notifier unexpected error")
