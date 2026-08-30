"""Tests for the CI.ME live-page platform adapter."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aioresponses import aioresponses

from linkstart.models import ChannelConfig, LiveInfo
from linkstart.platforms.cime import CimePlatform


FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://ci.me/@sample_channel/live"
API_URL = "https://ci.me/api/app/page/live/sample_channel"
PLAYBACK_URL = (
    "https://example.playback.live-video.net/api/video/channel.m3u8"
)
IVS_SESSION_PREFIX = (
    "https://streaming.cf.ci.me/ivs/v1/406692415290/AbCdEfGh/2026/6/4/12/34/XyZw"
)
THUMBNAIL_URL = f"{IVS_SESSION_PREFIX}/media/latest_thumbnail/thumb.jpg"
VOD_MASTER_URL = f"{IVS_SESSION_PREFIX}/media/hls/master.m3u8"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _api_live(**overrides) -> str:
    """The live-page API fixture with ``data.live`` fields overridden."""
    payload = json.loads(_fixture("cime_api_live.json"))
    payload["data"]["live"].update(overrides)
    return json.dumps(payload)


@pytest.fixture
def channel():
    return ChannelConfig(platform="cime", channel_id="sample_channel")


async def test_returns_live_info_from_api(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, body=_fixture("cime_api_live.json"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is not None
    assert info.live_id == "2026-06-04T12:34:56.000Z"
    assert info.title == "테스트 방송 / sample stream"
    assert info.url == PAGE_URL
    assert info.thumbnail_url == THUMBNAIL_URL
    assert info.started_at == datetime(2026, 6, 4, 12, 34, 56, tzinfo=timezone.utc)


async def test_returns_none_when_offline(channel):
    body = json.dumps({"code": 200, "data": {"live": None}})
    with aioresponses() as mocked:
        mocked.get(API_URL, body=body)
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_when_state_is_not_active(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, body=_api_live(state="ENDED"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_adult_live_without_playback_url_is_still_detected(
    channel, caplog
):
    """Regression: a live must not be silently skipped when the playback URL
    is missing (age-restricted broadcasts omit it from the public page)."""
    with aioresponses() as mocked:
        mocked.get(API_URL, body=_api_live(playback=None))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is not None
    assert info.live_id == "2026-06-04T12:34:56.000Z"
    assert any(
        "no playback url" in record.message.lower()
        for record in caplog.records
    )


async def test_build_url_uses_playback_url_from_api(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, body=_fixture("cime_api_live.json"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
            assert info is not None
            assert platform.build_url(channel, info) == PLAYBACK_URL
        finally:
            await platform.close()


async def test_build_full_url_derived_from_ivs_thumbnail(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, body=_fixture("cime_api_live.json"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
            assert info is not None
            assert platform.build_full_url(channel, info) == VOD_MASTER_URL
        finally:
            await platform.close()


async def test_build_full_url_none_when_thumbnail_is_not_ivs(channel):
    with aioresponses() as mocked:
        mocked.get(
            API_URL,
            body=_api_live(imageUrl="https://streaming.cf.ci.me/plain.jpg"),
        )
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
            assert info is not None
            assert platform.build_full_url(channel, info) is None
        finally:
            await platform.close()


def test_build_full_url_none_for_unknown_live(channel):
    platform = CimePlatform()
    live = LiveInfo(live_id="unknown", title="x", url=PAGE_URL)
    assert platform.build_full_url(channel, live) is None


def test_cime_uses_snapshot_dual_strategy(channel):
    from linkstart.downloader import Downloader
    from linkstart.downloader._snapshot_dual import SnapshotDualRecordingStrategy

    dl = Downloader()
    strategy = CimePlatform().recording_strategy(dl)
    assert isinstance(strategy, SnapshotDualRecordingStrategy)
    assert strategy.ctx is dl


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


async def test_returns_none_on_not_found(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, status=404)
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_http_error(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, status=500)
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_network_error(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, exception=ConnectionError("boom"))
        platform = CimePlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_malformed_json(channel):
    with aioresponses() as mocked:
        mocked.get(API_URL, body="<html>not json</html>")
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
