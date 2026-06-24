"""Tests for the Platform abstract base behaviors (default implementations)."""

import pytest

from linkstart.models import ChannelConfig, LiveInfo
from linkstart.platforms.base import Platform


class _FakePlatform(Platform):
    name = "fake"

    def __init__(self, results):
        self._results = list(results)

    async def check_live(self, channel):
        return self._results.pop(0) if self._results else None

    def build_url(self, channel, live):
        return f"https://fake/{channel.channel_id}"


@pytest.fixture
def channel():
    return ChannelConfig(platform="fake", channel_id="abc")


def test_default_supports_live_from_start_is_false():
    plat = _FakePlatform(results=[])
    assert plat.supports_live_from_start is False


def test_default_format_is_none():
    plat = _FakePlatform(results=[])
    assert plat.default_format is None


def test_default_download_profile_is_mp4_auto():
    plat = _FakePlatform(results=[])
    ch = ChannelConfig(platform="fake", channel_id="abc")
    profile = plat.download_profile(ch)
    assert profile.container == "mp4"
    assert profile.downloader == "auto"
    assert profile.to_yt_dlp_args() == []


async def test_is_still_live_true_when_same_id(channel):
    live = LiveInfo(live_id="999", title="t", url="https://x")
    plat = _FakePlatform(results=[live])
    assert await plat.is_still_live(channel, "999") is True


async def test_is_still_live_false_when_id_changed(channel):
    different = LiveInfo(live_id="other", title="t", url="https://x")
    plat = _FakePlatform(results=[different])
    assert await plat.is_still_live(channel, "999") is False


async def test_is_still_live_false_when_check_returns_none(channel):
    plat = _FakePlatform(results=[None])
    assert await plat.is_still_live(channel, "999") is False


def test_default_get_auth_cookies_is_none(channel):
    plat = _FakePlatform(results=[])
    assert plat.get_auth_cookies(channel) is None


async def test_default_close_is_noop():
    plat = _FakePlatform(results=[])
    # Must complete without raising.
    await plat.close()


async def test_default_validate_recording_returns_ok(tmp_path):
    plat = _FakePlatform(results=[])
    fake_file = tmp_path / "x.mp4"
    fake_file.write_bytes(b"")
    result = await plat.validate_recording(fake_file)
    assert result.status == "ok"
    assert result.reason is None


async def test_default_validate_recording_does_not_open_file(tmp_path):
    """Default impl is a no-op — must not require the file to exist."""
    plat = _FakePlatform(results=[])
    missing = tmp_path / "does-not-exist.mp4"
    result = await plat.validate_recording(missing)
    assert result.status == "ok"
