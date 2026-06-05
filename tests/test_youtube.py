"""Tests for the YouTube platform."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from linkstart.models import ChannelConfig
from linkstart.platforms.youtube import YoutubePlatform


@pytest.fixture
def channel():
    return ChannelConfig(platform="youtube", channel_id="@handle")


def test_youtube_supports_live_from_start():
    p = YoutubePlatform()
    assert p.supports_live_from_start is True


def test_youtube_default_format_set():
    p = YoutubePlatform()
    assert p.default_format == "137+140/bestvideo+bestaudio/best"


def test_youtube_yt_dlp_args_empty(channel):
    p = YoutubePlatform()
    assert p.yt_dlp_args(channel) == []


def test_build_url_handle(channel):
    p = YoutubePlatform()
    url = p.build_url(channel, live=None)  # type: ignore[arg-type]
    assert url == "https://www.youtube.com/@handle/live"


def test_build_url_channel_id():
    p = YoutubePlatform()
    ch = ChannelConfig(platform="youtube", channel_id="UCxxxx")
    url = p.build_url(ch, live=None)  # type: ignore[arg-type]
    assert url == "https://www.youtube.com/UCxxxx/live"


def _make_proc_mock(stdout: bytes, returncode: int = 0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


async def test_check_live_returns_info_when_is_live(channel):
    stdout = b"is_live\nVIDEO_ID\nLive title\nhttps://thumb.example/img.jpg\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)

    assert info is not None
    assert info.live_id == "VIDEO_ID"
    assert info.title == "Live title"
    assert info.url == "https://www.youtube.com/watch?v=VIDEO_ID"
    assert info.thumbnail_url == "https://thumb.example/img.jpg"


async def test_check_live_returns_none_when_not_live(channel):
    stdout = b"not_live\nVIDEO_ID\nSome title\nNA\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


async def test_check_live_returns_none_when_was_live(channel):
    stdout = b"was_live\nVIDEO_ID\nOld stream\n\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


async def test_check_live_returns_none_when_upcoming(channel):
    stdout = b"is_upcoming\nVIDEO_ID\nSoon\n\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


async def test_check_live_returns_none_on_yt_dlp_error(channel):
    proc = _make_proc_mock(b"", returncode=1)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


async def test_check_live_returns_none_on_malformed_output(channel):
    # Fewer than 3 lines.
    stdout = b"is_live\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


async def test_check_live_handles_missing_thumbnail(channel):
    # yt-dlp emits "NA" for missing fields; treat as no thumbnail.
    stdout = b"is_live\nVIDEO_ID\nLive title\nNA\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is not None
    assert info.thumbnail_url is None


async def test_check_live_passes_cookies_from_browser_when_set():
    ch = ChannelConfig(
        platform="youtube", channel_id="@handle", cookies_from_browser="chrome"
    )
    stdout = b"is_live\nVID\nT\nNA\n"
    proc = _make_proc_mock(stdout)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        await YoutubePlatform().check_live(ch)

    assert "--cookies-from-browser" in captured["args"]
    idx = list(captured["args"]).index("--cookies-from-browser")
    assert captured["args"][idx + 1] == "chrome"


async def test_check_live_parses_release_timestamp_as_started_at(channel):
    stdout = b"is_live\nVIDEO_ID\nLive title\nNA\n1735000000\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)

    assert info is not None
    assert info.started_at is not None
    assert info.started_at.timestamp() == 1735000000


async def test_check_live_started_at_none_when_release_timestamp_na(channel):
    stdout = b"is_live\nVIDEO_ID\nLive title\nNA\nNA\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)

    assert info is not None
    assert info.started_at is None


async def test_check_live_started_at_none_when_line_missing(channel):
    stdout = b"is_live\nVIDEO_ID\nLive title\nNA\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)

    assert info is not None
    assert info.started_at is None


async def test_check_live_returns_none_on_oserror(channel):
    """yt-dlp binary missing / exec failure → return None instead of raising."""
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        side_effect=OSError("yt-dlp not found"),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


async def test_check_live_returns_none_when_video_id_is_na(channel):
    stdout = b"is_live\nNA\nTitle\nNA\n"
    proc = _make_proc_mock(stdout)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await YoutubePlatform().check_live(channel)
    assert info is None


class _HungProc:
    """Fake yt-dlp process whose communicate() never returns on its own."""

    def __init__(self, *, ignore_terminate: bool = False):
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self._ignore_terminate = ignore_terminate
        self._done = asyncio.Event()

    async def communicate(self):
        await self._done.wait()
        return b"", b""

    def terminate(self):
        self.terminate_called = True
        if self._ignore_terminate:
            return
        if self.returncode is None:
            self.returncode = -15
        self._done.set()

    def kill(self):
        self.kill_called = True
        if self.returncode is None:
            self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


async def test_check_live_times_out_and_terminates_hung_yt_dlp(channel, monkeypatch):
    """A hung yt-dlp must not stall the worker forever: check_live returns None
    within the timeout and the child process is terminated."""
    monkeypatch.setattr(YoutubePlatform, "CHECK_LIVE_TIMEOUT", 0.05, raising=False)
    proc = _HungProc()
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await asyncio.wait_for(YoutubePlatform().check_live(channel), timeout=1.0)
    assert info is None
    assert proc.terminate_called is True


async def test_check_live_kills_yt_dlp_when_terminate_is_ignored(channel, monkeypatch):
    """If yt-dlp ignores SIGTERM after a timeout, escalate to SIGKILL."""
    monkeypatch.setattr(YoutubePlatform, "CHECK_LIVE_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(YoutubePlatform, "TERMINATE_TIMEOUT", 0.05, raising=False)
    proc = _HungProc(ignore_terminate=True)
    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        info = await asyncio.wait_for(YoutubePlatform().check_live(channel), timeout=1.0)
    assert info is None
    assert proc.terminate_called is True
    assert proc.kill_called is True


async def test_check_live_uses_configured_yt_dlp_bin(channel):
    """The yt-dlp binary must be injectable, mirroring Downloader(yt_dlp_bin=...)."""
    proc = _make_proc_mock(b"is_live\nVID\nT\nNA\n")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        await YoutubePlatform(yt_dlp_bin="/opt/yt-dlp-pinned").check_live(channel)

    assert captured["args"][0] == "/opt/yt-dlp-pinned"


async def test_check_live_omits_cookies_when_not_set(channel):
    stdout = b"is_live\nVID\nT\nNA\n"
    proc = _make_proc_mock(stdout)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch(
        "linkstart.platforms.youtube.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        await YoutubePlatform().check_live(channel)

    assert "--cookies-from-browser" not in captured["args"]
