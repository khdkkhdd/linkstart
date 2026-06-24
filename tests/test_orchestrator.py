import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkstart.config import AppConfig, Defaults, RawChannel
from linkstart.models import (
    ChannelConfig,
    DownloadResult,
    Event,
    EventType,
    LiveInfo,
    ValidationResult,
)
from linkstart.orchestrator import Orchestrator
from linkstart.platforms.base import Platform


class FakePlatform(Platform):
    name = "twitcasting"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def check_live(self, channel):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return None

    def build_url(self, channel, live):
        return f"https://x/{channel.channel_id}"


class RecordingNotifier:
    def __init__(self):
        self.events: list[Event] = []

    async def notify(self, event):
        self.events.append(event)

    async def close(self):
        pass


@pytest.fixture
def app_config(tmp_path):
    return AppConfig(
        defaults=Defaults(save_dir=tmp_path / "rec", poll_interval=0),
        channels=[
            RawChannel(platform="twitcasting", channel_id="abc", notifier="main"),
        ],
    )


@pytest.fixture
def state(tmp_state_path):
    from linkstart.state import StateStore
    return StateStore(tmp_state_path)


async def test_skips_when_not_live(app_config, state):
    plat = FakePlatform(results=[None, None])
    downloader = MagicMock()
    downloader.record = AsyncMock()
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 0
    assert notifier.events == []


async def test_downloads_when_new_live_detected(app_config, state, tmp_path):
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(
            success=True, file_path=tmp_path / "out.mp4",
            size_bytes=100, duration_sec=10, retry_count=0,
        )
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 1
    types = [e.type for e in notifier.events]
    assert EventType.LIVE_STARTED in types
    assert EventType.DOWNLOAD_STARTED in types
    assert EventType.DOWNLOAD_FINISHED in types


async def test_lifecycle_events_logged_to_console(app_config, state, tmp_path, caplog):
    """Key lifecycle transitions must appear in INFO-level logs, not only
    on Discord — so an operator watching the console can see what's going on."""
    import logging

    live = LiveInfo(live_id="999", title="show title", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(
            success=True, file_path=tmp_path / "out.mp4",
            size_bytes=12345, duration_sec=60, retry_count=2,
        )
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    with caplog.at_level(logging.INFO, logger="linkstart.orchestrator"):
        await asyncio.gather(orch.run(), stop_soon())

    msgs = [r.message for r in caplog.records]
    assert any("live detected" in m and "999" in m and "show title" in m for m in msgs)
    assert any("download finished" in m and "12345" in m for m in msgs)


async def test_failure_logged_at_warning(app_config, state, caplog):
    """Failed downloads must produce a warning-level log."""
    import logging

    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=False, error="boom", retry_count=3)
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    with caplog.at_level(logging.WARNING, logger="linkstart.orchestrator"):
        await asyncio.gather(orch.run(), stop_soon())

    msgs = [r.message for r in caplog.records]
    assert any("download failed" in m and "boom" in m for m in msgs)


async def test_records_again_while_same_live_stays_up(app_config, state, tmp_path):
    """A finished recording does not retire the live: if the platform still
    reports it (early-end misjudgment, post-stop restart), record the rest
    into a new session. While it is live, we record — no dedup."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live, live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=tmp_path / "out.mp4")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 2


async def test_records_live_seen_by_a_previous_run(app_config, state, tmp_path):
    """State from an earlier daemon run (any format) must never suppress
    recording of a still-live broadcast."""
    import json

    state.path.write_text(
        json.dumps({
            "channels": {
                "twitcasting:abc": {
                    "last_live_id": "999",
                    "last_seen_at": "2026-06-05T00:00:00+00:00",
                    "status": "done",
                }
            }
        }),
        encoding="utf-8",
    )
    from linkstart.state import StateStore
    state = StateStore(state.path)

    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=tmp_path / "out.mp4")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 1


async def test_state_journal_failure_does_not_stop_recording(app_config, state, tmp_path):
    """The journal is display-only: a failed write (e.g. disk full) must never
    prevent the recording itself, nor crash the worker (which would take the
    whole TaskGroup down)."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=tmp_path / "out.mp4")
    )
    notifier = RecordingNotifier()
    broken_state = MagicMock()
    broken_state.mark_seen = MagicMock(side_effect=OSError("disk full"))

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=broken_state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 1
    types = [e.type for e in notifier.events]
    assert EventType.DOWNLOAD_FINISHED in types


async def test_failed_download_is_retried_on_next_poll(app_config, state, tmp_path):
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live, live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        side_effect=[
            DownloadResult(success=False, error="boom"),
            DownloadResult(success=True, file_path=tmp_path / "out.mp4"),
        ]
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 2


async def test_repeated_same_live_failures_back_off(app_config, state):
    """A still-live broadcast that keeps failing must not be re-attempted on
    every poll forever. The first retry is free; after that, the backoff policy
    blocks further immediate attempts within the (fast-test) time window."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live, live, live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=False, error="stall")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    # initial attempt + one free retry; the 3rd detection is held off by backoff.
    assert downloader.record.await_count == 2


async def test_live_started_notified_once_across_retries(app_config, state):
    """Retrying the same live (failure loop) must not spam LIVE_STARTED /
    DOWNLOAD_STARTED on every poll — announce each live_id once per process."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live, live, live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=False, error="boom")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    # Disable the ERROR cooldown so every failure notifies (tested separately).
    orch.ERROR_DEDUP_SEC = 0

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count >= 2
    types = [e.type for e in notifier.events]
    assert types.count(EventType.LIVE_STARTED) == 1
    assert types.count(EventType.DOWNLOAD_STARTED) == 1
    # Each failure still surfaces as an ERROR.
    assert types.count(EventType.ERROR) >= 2


async def test_error_notifications_are_rate_limited(app_config, state):
    """Repeated failures of the same live within the cooldown window must not
    spam Discord — one ERROR per window (the failures are still logged)."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live] * 10)
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=False, error="boom")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count >= 2
    types = [e.type for e in notifier.events]
    assert types.count(EventType.ERROR) == 1


async def test_state_keyed_by_channel_id_even_with_alias(tmp_path, state):
    """Dedup state must be keyed by the immutable channel_id, NOT log_name.
    log_name embeds the alias, so keying by it breaks `linkstart list` lookups
    and resets dedup whenever the operator adds/changes an alias."""
    cfg = AppConfig(
        defaults=Defaults(save_dir=tmp_path / "rec", poll_interval=0),
        channels=[
            RawChannel(
                platform="twitcasting", channel_id="abc",
                notifier="main", alias="My Channel",
            ),
        ],
    )
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=tmp_path / "out.mp4")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=cfg,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert downloader.record.await_count == 1
    # The entry must be retrievable by channel_id (what `linkstart list` uses).
    entry = state.get_entry("twitcasting", "abc")
    assert entry is not None
    assert entry["last_live_id"] == "999"


async def test_unknown_platform_skipped(tmp_path, state):
    cfg = AppConfig(
        defaults=Defaults(save_dir=tmp_path / "rec", poll_interval=0),
        channels=[
            RawChannel(platform="nonexistent", channel_id="x", notifier="main"),
            RawChannel(platform="twitcasting", channel_id="abc", notifier="main"),
        ],
    )
    live = LiveInfo(live_id="1", title="", url="")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=tmp_path / "x.mp4")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=cfg,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())
    # Only twitcasting worker did anything
    assert downloader.record.await_count == 1


async def test_unknown_notifier_id_logs_warning(app_config, state, caplog):
    import logging

    # app_config's channel references notifier_id "main", but we pass empty notifiers dict
    plat = FakePlatform(results=[None])
    downloader = MagicMock()
    downloader.record = AsyncMock()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={},  # no notifiers registered
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        orch.stop()

    with caplog.at_level(logging.WARNING, logger="linkstart.orchestrator"):
        await asyncio.gather(orch.run(), stop_soon())

    assert any(
        "unknown notifier_id" in rec.message and "main" in rec.message
        for rec in caplog.records
    )


async def test_emits_error_on_failed_download(app_config, state):
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=False, error="boom")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())
    types = [e.type for e in notifier.events]
    assert EventType.ERROR in types


async def test_check_live_exception_is_logged_and_loop_continues(app_config, state, caplog):
    """An exception inside platform.check_live must be caught; worker keeps polling."""
    import logging

    class RaisingPlatform(Platform):
        name = "twitcasting"

        def __init__(self):
            self.calls = 0

        async def check_live(self, channel):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return None

        def build_url(self, channel, live):
            return ""

    plat = RaisingPlatform()
    downloader = MagicMock()
    downloader.record = AsyncMock()
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        orch.stop()

    with caplog.at_level(logging.ERROR, logger="linkstart.orchestrator"):
        await asyncio.gather(orch.run(), stop_soon())

    assert plat.calls >= 1
    assert downloader.record.await_count == 0
    assert any("check_live raised unexpectedly" in r.message for r in caplog.records)


async def test_worker_iteration_exception_does_not_kill_other_channels(
    app_config, state, caplog
):
    """An unexpected exception in one channel's per-iteration body must NOT escape
    to the TaskGroup (which would cancel every other channel's worker). It must be
    logged, and that worker must keep polling so it self-heals."""
    import logging

    class AlwaysLivePlatform(Platform):
        name = "twitcasting"

        def __init__(self, live):
            self._live = live
            self.calls = 0

        async def check_live(self, channel):
            self.calls += 1
            return self._live

        def build_url(self, channel, live):
            return "https://x"

    class RaisingRetryPolicy:
        """should_attempt raises every time — simulates an unexpected bug in the
        per-iteration bookkeeping, not in check_live (which is already guarded)."""

        def __init__(self):
            self.calls = 0

        def should_attempt(self, channel, live_id, *, now):
            self.calls += 1
            raise RuntimeError("unexpected per-iteration failure")

        def record_success(self, *a, **k):
            pass

        def record_failure(self, *a, **k):
            pass

    live = LiveInfo(live_id="1", title="t", url="https://x")
    plat = AlwaysLivePlatform(live)
    downloader = MagicMock()
    downloader.record = AsyncMock()
    policy = RaisingRetryPolicy()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": RecordingNotifier()},
        downloader=downloader,
        state=state,
        retry_policy=policy,
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        orch.stop()

    # Must NOT raise: without isolation the RuntimeError propagates out of the
    # worker, the TaskGroup cancels siblings, and run() raises an ExceptionGroup.
    with caplog.at_level(logging.ERROR, logger="linkstart.orchestrator"):
        await asyncio.gather(orch.run(), stop_soon())

    # The worker kept polling across the repeated failure rather than dying once.
    assert policy.calls >= 2
    assert any("worker iteration failed" in r.message for r in caplog.records)


async def test_wait_timeout_path_keeps_polling(tmp_path, state):
    """When stop is NOT signalled within poll_interval, _wait must return False
    and the loop continues (covers asyncio.TimeoutError branch)."""
    cfg = AppConfig(
        defaults=Defaults(save_dir=tmp_path / "rec", poll_interval=0),  # not used directly
        channels=[
            RawChannel(platform="twitcasting", channel_id="abc", notifier="main"),
        ],
    )
    plat = FakePlatform(results=[None, None, None, None])
    downloader = MagicMock()
    downloader.record = AsyncMock()
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=cfg,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )
    # Directly exercise _wait with positive seconds, asserting both timeout-False
    # and stop-True return values.
    short_wait = asyncio.create_task(orch._wait(0.05))
    result_timeout = await short_wait
    assert result_timeout is False   # TimeoutError → False

    # Now signal stop, verify _wait returns True immediately.
    orch.stop()
    result_stop = await orch._wait(10)
    assert result_stop is True


async def test_notifications_with_none_notifier_are_noop():
    """A channel without a notifier must run silently — no sends, no raises."""
    from linkstart.cooldown import Cooldown
    from linkstart.orchestrator import ChannelNotifications

    channel = ChannelConfig(platform="x", channel_id="y")
    live = LiveInfo(live_id="1", title="t", url="https://x")
    notifications = ChannelNotifications(None, channel, Cooldown(0))

    await notifications.announce_live(live)
    await notifications.error(live, "boom")
    await notifications.interrupted(
        Event(EventType.DOWNLOAD_INTERRUPTED, channel, live=live)
    )


async def test_fire_swallows_notifier_exception(app_config, state):
    """If notifier.notify raises, _fire must catch it (recording continues)."""

    class ExplodingNotifier:
        async def notify(self, event):
            raise RuntimeError("notifier broken")

        async def close(self):
            pass

    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=Path("/tmp/x.mp4"))
    )
    notifier = ExplodingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},  # type: ignore[arg-type]
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    # Must not raise out of run().
    await asyncio.gather(orch.run(), stop_soon())


async def test_emits_error_when_downloader_raises(app_config, state):
    """If Downloader.record raises, orchestrator must fire ERROR and keep running."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(side_effect=RuntimeError("kaboom"))
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    types = [e.type for e in notifier.events]
    assert EventType.ERROR in types
    err = next(e for e in notifier.events if e.type == EventType.ERROR)
    assert err.message == "kaboom"
    assert err.live is live


async def test_stop_during_wait_after_downloader_raise_returns(tmp_path, state):
    """If stop is signalled DURING _wait after downloader raised, return early."""
    cfg = AppConfig(
        defaults=Defaults(save_dir=tmp_path / "rec", poll_interval=10),
        channels=[
            RawChannel(platform="twitcasting", channel_id="abc", notifier="main"),
        ],
    )
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(side_effect=RuntimeError("kaboom"))
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=cfg,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_after_error():
        # Wait for the ERROR event to fire, then signal stop while orch is in _wait.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any(e.type == EventType.ERROR for e in notifier.events):
                orch.stop()
                return

    await asyncio.wait_for(asyncio.gather(orch.run(), stop_after_error()), timeout=2.0)


async def test_stop_during_wait_after_successful_download_returns(tmp_path, state):
    """If stop is signalled DURING _wait after a successful download, return early."""
    cfg = AppConfig(
        defaults=Defaults(save_dir=tmp_path / "rec", poll_interval=10),
        channels=[
            RawChannel(platform="twitcasting", channel_id="abc", notifier="main"),
        ],
    )
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(success=True, file_path=tmp_path / "out.mp4")
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=cfg,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_after_finish():
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any(e.type == EventType.DOWNLOAD_FINISHED for e in notifier.events):
                orch.stop()
                return

    await asyncio.wait_for(asyncio.gather(orch.run(), stop_after_finish()), timeout=2.0)


async def test_orchestrator_passes_on_interrupted_callback(app_config, state, tmp_path):
    """When Downloader.record fires the interrupted callback, the notifier sees it."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    notifier = RecordingNotifier()

    interrupt_captured = {"called": False}

    class CapturingDownloader:
        async def record(self, channel, platform, live, on_interrupted=None, stop_event=None):
            if on_interrupted is not None:
                await on_interrupted(
                    Event(
                        type=EventType.DOWNLOAD_INTERRUPTED,
                        channel=channel,
                        live=live,
                        message="loop: edge   attempt: 1",
                    )
                )
                interrupt_captured["called"] = True
            return DownloadResult(
                success=True, file_path=tmp_path / "out.mp4",
                size_bytes=1, duration_sec=1, retry_count=1,
            )

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=CapturingDownloader(),
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert interrupt_captured["called"] is True
    types = [e.type for e in notifier.events]
    assert EventType.DOWNLOAD_INTERRUPTED in types


# --- validation-driven branching --------------------------------------------

async def test_invalid_validation_fires_error_not_finished(app_config, state, tmp_path):
    """A successful download whose validation flags it as 'invalid' (e.g.
    TwitCasting login-wall placeholder) must be surfaced as an ERROR event,
    not a DOWNLOAD_FINISHED — the file on disk is misleading junk."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    file_path = tmp_path / "out.mp4"
    file_path.write_bytes(b"junk")
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(
            success=True,
            file_path=file_path,
            size_bytes=100,
            duration_sec=4278,
            validation=ValidationResult(
                status="invalid",
                reason="likely login wall: 52 kbps over 4278s",
            ),
        )
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    types = [e.type for e in notifier.events]
    assert EventType.DOWNLOAD_FINISHED not in types
    assert EventType.ERROR in types
    err = next(e for e in notifier.events if e.type == EventType.ERROR)
    assert "login wall" in (err.message or "")
    # File path must be carried so the operator can find the junk file.
    assert str(file_path) in (err.message or "")


async def test_ok_validation_still_fires_download_finished(app_config, state, tmp_path):
    """Successful download with ok validation behaves as before."""
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])
    file_path = tmp_path / "out.mp4"
    file_path.write_bytes(b"data")
    downloader = MagicMock()
    downloader.record = AsyncMock(
        return_value=DownloadResult(
            success=True,
            file_path=file_path,
            size_bytes=100,
            duration_sec=10,
            validation=ValidationResult(status="ok"),
        )
    )
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
    )

    async def stop_soon():
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    types = [e.type for e in notifier.events]
    assert EventType.DOWNLOAD_FINISHED in types
    assert EventType.ERROR not in types


async def test_record_failure_timestamp_is_after_the_attempt(app_config, state, tmp_path):
    """Backoff must be measured from when a failed attempt ENDED, not when it
    started. The old code captured `now` once (before _record_live) and reused
    it for record_failure, so a long-running failure consumed its own backoff
    delay → the daemon re-attempts immediately, defeating the throttle."""
    from linkstart.retry import LiveRetryPolicy

    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = FakePlatform(results=[live])

    class SpyPolicy(LiveRetryPolicy):
        def __init__(self):
            self.attempt_now: list[float] = []
            self.failure_now: list[float] = []

        def should_attempt(self, channel, live_id, *, now):
            self.attempt_now.append(now)
            return True

        def record_failure(self, channel, live_id, *, now):
            self.failure_now.append(now)

        def record_success(self, channel, live_id):
            pass

    spy = SpyPolicy()

    async def slow_failing_record(*a, **k):
        await asyncio.sleep(0.05)  # attempt takes real time → loop clock advances
        return DownloadResult(success=False, error="boom")

    downloader = MagicMock()
    downloader.record = AsyncMock(side_effect=slow_failing_record)
    notifier = RecordingNotifier()

    orch = Orchestrator(
        config=app_config,
        platforms={"twitcasting": plat},
        notifiers={"main": notifier},
        downloader=downloader,
        state=state,
        retry_policy=spy,
    )

    async def stop_soon():
        await asyncio.sleep(0.2)
        orch.stop()

    await asyncio.gather(orch.run(), stop_soon())

    assert spy.attempt_now and spy.failure_now
    # The failure must be timestamped after the ~0.05s attempt, not with the
    # pre-attempt time.
    assert spy.failure_now[0] > spy.attempt_now[0]
