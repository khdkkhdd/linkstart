"""Tests for the CI.ME live-page platform adapter."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aioresponses import aioresponses

from linkstart.models import ChannelConfig, LiveInfo
from linkstart.platforms.cime import CimePlatform


FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://ci.me/@sample_channel/live"
PLAYBACK_URL = (
    "https://example.playback.live-video.net/api/video/channel.m3u8"
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def channel():
    return ChannelConfig(platform="cime", channel_id="sample_channel")


async def test_returns_live_info_from_json_ld(channel):
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, body=_fixture("cime_live.html"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is not None
    assert info.live_id == "2026-06-04T12:34:56.000Z"
    assert info.title == "테스트 방송 / sample stream"
    assert info.url == PAGE_URL
    assert info.thumbnail_url == "https://streaming.cf.ci.me/thumb.jpg"
    assert info.started_at == datetime(2026, 6, 4, 12, 34, 56, tzinfo=timezone.utc)


async def test_returns_none_when_page_has_no_live_video(channel):
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, body=_fixture("cime_offline.html"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_when_broadcast_event_is_not_live(channel):
    html = _fixture("cime_live.html").replace(
        '"isLiveBroadcast": true', '"isLiveBroadcast": false'
    )
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, body=html)
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_build_url_uses_discovered_hls_master(channel):
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, body=_fixture("cime_live.html"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
            assert info is not None
            assert platform.build_url(channel, info) == PLAYBACK_URL
        finally:
            await platform.close()


def test_build_url_falls_back_to_public_page(channel):
    platform = CimePlatform()
    live = LiveInfo(live_id="unknown", title="x", url=PAGE_URL)
    assert platform.build_url(channel, live) == PAGE_URL


def test_download_profile_is_resilient_native_hls(channel):
    profile = CimePlatform().download_profile(channel)
    assert profile.container == "mpegts"
    assert profile.part_suffix == ".ts"
    assert profile.downloader == "native"
    assert profile.to_yt_dlp_args() == [
        "--hls-use-mpegts",
        "--hls-prefer-native",
        "--referer",
        PAGE_URL,
    ]


def test_accepts_channel_id_with_at_prefix():
    channel = ChannelConfig(platform="cime", channel_id="@sample_channel")
    platform = CimePlatform()
    live = LiveInfo(live_id="unknown", title="x", url=PAGE_URL)
    assert platform.build_url(channel, live) == PAGE_URL


async def test_returns_none_on_http_error(channel):
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, status=500)
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_network_error(channel):
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, exception=ConnectionError("boom"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_ignores_malformed_json_ld(channel):
    html = '<script type="application/ld+json">{not json}</script>'
    with aioresponses() as mocked:
        mocked.get(PAGE_URL, body=html)
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


def test_get_auth_cookies_with_browser(monkeypatch):
    channel = ChannelConfig(
        platform="cime", channel_id="sample_channel", cookies_from_browser="firefox"
    )
    captured: dict[str, str] = {}

    def fake_get(domain, browser):
        captured.update(domain=domain, browser=browser)
        return {"session-id": "token"}

    monkeypatch.setattr("linkstart.platforms.cime.get_browser_cookies", fake_get)
    assert CimePlatform().get_auth_cookies(channel) == {"session-id": "token"}
    assert captured == {"domain": ".ci.me", "browser": "firefox"}


async def test_owned_session_is_tuned_and_reused():
    from linkstart.platforms._http import DNS_CACHE_TTL_S, KEEPALIVE_TIMEOUT_S

    platform = CimePlatform()
    try:
        first = await platform._get_session()
        second = await platform._get_session()
        assert first is second
        assert first.connector._keepalive_timeout == KEEPALIVE_TIMEOUT_S
        assert first.connector._cached_hosts._ttl == DNS_CACHE_TTL_S
        assert "Mozilla/5.0" in first.headers["User-Agent"]
        assert first.headers["Accept-Language"].startswith("ko-KR")
    finally:
        await platform.close()
