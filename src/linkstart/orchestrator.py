"""Top-level coordinator: one async worker per channel."""
import asyncio
import logging
from pathlib import Path

from linkstart.config import AppConfig, merge_channel
from linkstart.cooldown import Cooldown
from linkstart.downloader import Downloader
from linkstart.models import (
    ChannelConfig,
    DownloadResult,
    Event,
    EventType,
    LiveInfo,
)
from linkstart.notifier.base import Notifier
from linkstart.platforms.base import Platform
from linkstart.state import StateStore

log = logging.getLogger(__name__)


class ChannelNotifications:
    """Per-channel notification policy: announce each live once, rate-limit
    errors, never let a notifier failure reach the recording flow."""

    def __init__(
        self,
        notifier: Notifier | None,
        channel: ChannelConfig,
        error_cooldown: Cooldown,
    ) -> None:
        self._notifier = notifier
        self._channel = channel
        self._error_cooldown = error_cooldown
        self._announced_live_id: str | None = None

    async def announce_live(self, live: LiveInfo) -> None:
        if live.live_id == self._announced_live_id:
            return
        self._announced_live_id = live.live_id
        await self._send(Event(EventType.LIVE_STARTED, self._channel, live=live))
        await self._send(Event(EventType.DOWNLOAD_STARTED, self._channel, live=live))

    async def interrupted(self, event: Event) -> None:
        await self._send(event)

    async def finished(self, live: LiveInfo, result: DownloadResult) -> None:
        await self._send(
            Event(
                EventType.DOWNLOAD_FINISHED,
                self._channel,
                live=live,
                file_path=result.file_path,
                size_bytes=result.size_bytes,
                duration_sec=result.duration_sec,
                retry_count=result.retry_count,
            )
        )

    async def error(
        self,
        live: LiveInfo | None,
        message: str | None,
        *,
        file_path: Path | None = None,
        retry_count: int = 0,
    ) -> None:
        if not self._error_cooldown.try_acquire():
            log.debug("error notification suppressed (cooldown): %s", message)
            return
        await self._send(
            Event(
                EventType.ERROR,
                self._channel,
                live=live,
                message=message,
                file_path=file_path,
                retry_count=retry_count,
            )
        )

    async def _send(self, event: Event) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.notify(event)
        except Exception:
            log.exception("notifier failed")


class Orchestrator:
    ERROR_DEDUP_SEC: float = 300.0

    def __init__(
        self,
        config: AppConfig,
        platforms: dict[str, Platform],
        notifiers: dict[str, Notifier],
        downloader: Downloader,
        state: StateStore,
    ) -> None:
        self.config = config
        self.platforms = platforms
        self.notifiers = notifiers
        self.downloader = downloader
        self.state = state
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        channels = [
            merge_channel(raw, self.config.defaults) for raw in self.config.channels
        ]
        for ch in channels:
            if ch.notifier_id and ch.notifier_id not in self.notifiers:
                log.warning(
                    "channel %s/%s references unknown notifier_id %r; events will be dropped",
                    ch.platform, ch.channel_id, ch.notifier_id,
                )
        async with asyncio.TaskGroup() as tg:
            for ch in channels:
                if ch.platform not in self.platforms:
                    log.warning(
                        "skipping channel: unknown platform %s", ch.platform
                    )
                    continue
                tg.create_task(self._worker(ch))

    async def _worker(self, channel: ChannelConfig) -> None:
        platform = self.platforms[channel.platform]
        notifications = ChannelNotifications(
            self.notifiers.get(channel.notifier_id) if channel.notifier_id else None,
            channel,
            Cooldown(self.ERROR_DEDUP_SEC),
        )
        log.info(
            "worker started for %s/%s", channel.platform, channel.log_name
        )
        while not self._stop.is_set():
            live = await self._check_live(platform, channel)
            if live is not None:
                await self._record_live(channel, platform, notifications, live)
            if await self._wait(channel.poll_interval):
                return

    async def _check_live(
        self, platform: Platform, channel: ChannelConfig
    ) -> LiveInfo | None:
        try:
            return await platform.check_live(channel)
        except Exception:
            log.exception("check_live raised unexpectedly")
            return None

    async def _record_live(
        self,
        channel: ChannelConfig,
        platform: Platform,
        notifications: ChannelNotifications,
        live: LiveInfo,
    ) -> None:
        try:
            self.state.mark_seen(channel.platform, channel.channel_id, live.live_id)
        except Exception:
            log.exception("state journal write failed; recording continues")
        log.info(
            "live detected: %s/%s id=%s title=%r",
            channel.platform, channel.log_name, live.live_id, live.title,
        )
        await notifications.announce_live(live)

        async def forward_interrupted(event: Event) -> None:
            log.info(
                "download interrupted: %s/%s %s",
                channel.platform, channel.log_name, event.message,
            )
            await notifications.interrupted(event)

        try:
            result = await self.downloader.record(
                channel, platform, live,
                on_interrupted=forward_interrupted,
                stop_event=self._stop,
            )
        except Exception as e:
            log.exception("downloader raised")
            await notifications.error(live, str(e))
            return

        if result.success and result.validation.status == "invalid":
            # yt-dlp produced a file but the platform flagged it as a
            # silent failure (e.g. TwitCasting login-wall placeholder).
            # Surface as an error so the operator can investigate; keep
            # the file on disk for inspection rather than auto-deleting.
            detail = (
                f"{result.validation.reason} [file: {result.file_path}]"
                if result.validation.reason
                else f"validation invalid [file: {result.file_path}]"
            )
            log.warning(
                "download flagged invalid: %s/%s %s",
                channel.platform, channel.log_name, detail,
            )
            await notifications.error(
                live, detail,
                file_path=result.file_path, retry_count=result.retry_count,
            )
        elif result.success:
            log.info(
                "download finished: %s/%s file=%s size=%s duration=%ss retries=%s",
                channel.platform, channel.log_name,
                result.file_path, result.size_bytes, result.duration_sec,
                result.retry_count,
            )
            await notifications.finished(live, result)
        else:
            log.warning(
                "download failed: %s/%s error=%s retries=%s",
                channel.platform, channel.log_name,
                result.error, result.retry_count,
            )
            await notifications.error(live, result.error, retry_count=result.retry_count)

    async def _wait(self, seconds: int) -> bool:
        """Return True if stop was requested during the wait."""
        if seconds <= 0:
            # Yield control so other tasks (e.g. stop signaller) can run,
            # but do not actually block.
            await asyncio.sleep(0)
            return self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False
