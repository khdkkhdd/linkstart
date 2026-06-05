"""Tests for the Chzzk (Naver) platform."""
import json
from pathlib import Path

import pytest
from aioresponses import aioresponses

from linkstart.models import ChannelConfig
from linkstart.platforms.chzzk import ChzzkPlatform


FIXTURES = Path(__file__).parent / "fixtures"
CHANNEL_ID = "abcdef0123456789abcdef0123456789"
LIVE_DETAIL_URL = (
    f"https://api.chzzk.naver.com/service/v2/channels/{CHANNEL_ID}/live-detail"
)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def channel():
    return ChannelConfig(platform="chzzk", channel_id=CHANNEL_ID)


def test_supports_live_from_start_false():
    p = ChzzkPlatform()
    assert p.supports_live_from_start is False


def test_build_url(channel):
    p = ChzzkPlatform()
    url = p.build_url(channel, live=None)  # type: ignore[arg-type]
    assert url == f"https://chzzk.naver.com/live/{CHANNEL_ID}"


def test_yt_dlp_args(channel):
    p = ChzzkPlatform()
    assert p.yt_dlp_args(channel) == ["--hls-use-mpegts"]


async def test_returns_live_info_when_status_open(channel):
    body = _load_fixture("chzzk_live.json")
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, payload=body)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is not None
    assert info.live_id == "1234567"
    assert info.title == "테스트 방송 / sample stream"
    assert info.url == f"https://chzzk.naver.com/live/{CHANNEL_ID}"
    assert info.thumbnail_url == "https://chzzk.example/thumb.jpg"


async def test_returns_none_when_status_close(channel):
    body = _load_fixture("chzzk_offline.json")
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, payload=body)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is None


async def test_returns_none_on_http_error(channel):
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, status=500)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is None


async def test_returns_none_on_network_error(channel):
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, exception=ConnectionError("boom"))
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is None


async def test_returns_none_on_malformed_json(channel):
    with aioresponses() as m:
        m.get(
            LIVE_DETAIL_URL,
            status=200,
            body="<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is None


async def test_returns_none_when_live_id_missing(channel):
    body = {
        "code": 200,
        "content": {
            "liveId": None,
            "liveTitle": "X",
            "status": "OPEN",
        },
    }
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, payload=body)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is None


async def test_returns_none_when_content_missing(channel):
    body = {"code": 200, "content": None}
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, payload=body)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is None


async def test_accepts_string_live_id(channel):
    # Defensive: API ordinarily returns int, but tolerate string too.
    body = {
        "code": 200,
        "content": {
            "liveId": "42",
            "liveTitle": "x",
            "status": "OPEN",
        },
    }
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, payload=body)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(channel)
        finally:
            await p.close()

    assert info is not None
    assert info.live_id == "42"


def test_get_auth_cookies_without_browser_returns_none(channel):
    p = ChzzkPlatform()
    # cookies_from_browser is None by default.
    assert p.get_auth_cookies(channel) is None


def test_get_auth_cookies_with_browser(monkeypatch):
    ch = ChannelConfig(
        platform="chzzk", channel_id=CHANNEL_ID, cookies_from_browser="chrome"
    )
    captured: dict[str, object] = {}

    def fake_get(domain, browser):
        captured["domain"] = domain
        captured["browser"] = browser
        return {"NID_AUT": "tok", "NID_SES": "sess"}

    monkeypatch.setattr("linkstart.platforms.chzzk.get_browser_cookies", fake_get)
    p = ChzzkPlatform()
    cookies = p.get_auth_cookies(ch)
    assert cookies == {"NID_AUT": "tok", "NID_SES": "sess"}
    assert captured == {"domain": ".naver.com", "browser": "chrome"}


def test_get_auth_cookies_empty_dict_returns_none(monkeypatch):
    ch = ChannelConfig(
        platform="chzzk", channel_id=CHANNEL_ID, cookies_from_browser="chrome"
    )
    monkeypatch.setattr(
        "linkstart.platforms.chzzk.get_browser_cookies", lambda domain, browser: {}
    )
    p = ChzzkPlatform()
    assert p.get_auth_cookies(ch) is None


async def test_check_live_attaches_auth_cookies(monkeypatch):
    ch = ChannelConfig(
        platform="chzzk", channel_id=CHANNEL_ID, cookies_from_browser="chrome"
    )
    monkeypatch.setattr(
        "linkstart.platforms.chzzk.get_browser_cookies",
        lambda domain, browser: {"NID_AUT": "tok"},
    )
    body = _load_fixture("chzzk_live.json")
    with aioresponses() as m:
        m.get(LIVE_DETAIL_URL, payload=body)
        p = ChzzkPlatform()
        try:
            info = await p.check_live(ch)
        finally:
            await p.close()
    # If cookies were rejected we would still get LiveInfo here (aioresponses
    # doesn't gate on cookies). This test mainly verifies the cookie path
    # doesn't crash and the request is made successfully.
    assert info is not None
