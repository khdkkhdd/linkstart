import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

from linkstart.models import ChannelConfig, LiveInfo
from linkstart.platforms.twitcasting import TwitcastingPlatform


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def channel():
    return ChannelConfig(platform="twitcasting", channel_id="abc")


async def test_returns_live_info_when_live(channel):
    body = _load_fixture("twitcasting_live.json")
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            payload=body,
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is not None
    assert info.live_id == "987654321"
    assert info.title == "테스트 방송 / sample stream"
    assert info.url == "https://twitcasting.tv/abc"


async def test_returns_none_when_offline(channel):
    body = _load_fixture("twitcasting_offline.json")
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            payload=body,
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_http_error(channel):
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            status=500,
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_network_error(channel):
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            exception=ConnectionError("boom"),
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


def test_build_url(channel):
    platform = TwitcastingPlatform()
    live = LiveInfo(
        live_id="987654321",
        title="t",
        url="https://twitcasting.tv/abc",
    )
    url = platform.build_url(channel, live)
    # Pin to the specific movie so yt-dlp does not re-resolve the channel's
    # "current movie" (which can be a stale id across broadcast transitions).
    assert url == "https://twitcasting.tv/abc/movie/987654321"


def test_download_profile_is_mpegts(channel):
    platform = TwitcastingPlatform()
    # --live-from-start is controlled by the Downloader, not the platform.
    # The platform declares an mpegts container (→ .ts parts + --hls-use-mpegts).
    profile = platform.download_profile(channel)
    assert profile.container == "mpegts"
    assert profile.part_suffix == ".ts"
    assert profile.to_yt_dlp_args() == ["--hls-use-mpegts"]


def test_channel_downloader_override_applies():
    # A per-channel `downloader: native` overrides the downloader engine while
    # preserving the platform-intrinsic container — lets the operator switch to
    # yt-dlp's native HLS downloader (for fMP4 streams) without a code change.
    ch = ChannelConfig(platform="twitcasting", channel_id="abc", downloader="native")
    profile = TwitcastingPlatform().download_profile(ch)
    assert profile.downloader == "native"
    assert profile.container == "mpegts"  # intrinsic, unchanged
    assert "--hls-prefer-native" in profile.to_yt_dlp_args()


def test_supports_live_from_start_false(channel):
    platform = TwitcastingPlatform()
    assert platform.supports_live_from_start is False


async def test_returns_none_when_live_true_but_id_missing(channel):
    body = _load_fixture("twitcasting_live_no_id.json")
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            payload=body,
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_returns_none_on_malformed_json(channel):
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            status=200,
            body="<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is None


async def test_accepts_id_zero(channel):
    # Movie id 0 is theoretical but the implementation should not reject it.
    body = {"movie": {"id": 0, "live": True, "title": "edge"}}
    with aioresponses() as m:
        m.get(
            f"{TwitcastingPlatform.STREAMSERVER_URL}?target=abc&mode=client",
            payload=body,
        )
        platform = TwitcastingPlatform()
        try:
            info = await platform.check_live(channel)
        finally:
            await platform.close()

    assert info is not None
    assert info.live_id == "0"


# ---------- validate_recording ---------------------------------------------
# Background: TwitCasting serves a placeholder video (login-wall) over HLS
# when a viewer is not authenticated for a login-only broadcast. yt-dlp
# silently records it as a normal-looking but very-low-bitrate mp4. The
# observed real-world signature: duration ~71min, bit_rate ~52 kbps.


async def test_validate_recording_ok_for_normal_stream(tmp_path):
    platform = TwitcastingPlatform()
    f = tmp_path / "ok.mp4"
    f.write_bytes(b"x")

    with patch.object(
        platform, "_ffprobe_metrics",
        new=AsyncMock(return_value={"duration_s": 3600.0, "bitrate_kbps": 320.0}),
    ):
        result = await platform.validate_recording(f)

    assert result.status == "ok"
    assert result.reason is None
    assert result.metrics["bitrate_kbps"] == 320.0


async def test_validate_recording_invalid_on_login_wall_signature(tmp_path):
    platform = TwitcastingPlatform()
    f = tmp_path / "wall.mp4"
    f.write_bytes(b"x")

    with patch.object(
        platform, "_ffprobe_metrics",
        new=AsyncMock(return_value={"duration_s": 4278.0, "bitrate_kbps": 52.0}),
    ):
        result = await platform.validate_recording(f)

    assert result.status == "invalid"
    assert "login" in (result.reason or "").lower()
    assert result.metrics["bitrate_kbps"] == 52.0
    assert result.metrics["duration_s"] == 4278.0


async def test_validate_recording_short_low_bitrate_is_ok(tmp_path):
    """A real broadcast that ended after a few seconds may have low bitrate
    statistically — duration filter prevents false positives on short clips.
    """
    platform = TwitcastingPlatform()
    f = tmp_path / "short.mp4"
    f.write_bytes(b"x")

    with patch.object(
        platform, "_ffprobe_metrics",
        new=AsyncMock(return_value={"duration_s": 30.0, "bitrate_kbps": 40.0}),
    ):
        result = await platform.validate_recording(f)

    assert result.status == "ok"


async def test_validate_recording_when_probe_fails_returns_ok(tmp_path):
    """If ffprobe can't read the file, don't false-positive — we never
    treat 'cannot validate' as a failure."""
    platform = TwitcastingPlatform()
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")

    with patch.object(
        platform, "_ffprobe_metrics", new=AsyncMock(return_value=None),
    ):
        result = await platform.validate_recording(f)

    assert result.status == "ok"


async def test_ffprobe_metrics_parses_json_output(tmp_path):
    platform = TwitcastingPlatform()
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = json.dumps({
                "format": {"duration": "120.5", "bit_rate": "256000"}
            }).encode()
            return payload, b""

    with patch(
        "linkstart.platforms.twitcasting.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_Proc()),
    ):
        metrics = await platform._ffprobe_metrics(f)

    assert metrics is not None
    assert metrics["duration_s"] == 120.5
    assert metrics["bitrate_kbps"] == 256.0


async def test_ffprobe_metrics_returns_none_on_nonzero_exit(tmp_path):
    platform = TwitcastingPlatform()
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")

    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"ffprobe: not found"

    with patch(
        "linkstart.platforms.twitcasting.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_Proc()),
    ):
        metrics = await platform._ffprobe_metrics(f)

    assert metrics is None


async def test_ffprobe_metrics_returns_none_on_malformed_output(tmp_path):
    platform = TwitcastingPlatform()
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"<not json>", b""

    with patch(
        "linkstart.platforms.twitcasting.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_Proc()),
    ):
        metrics = await platform._ffprobe_metrics(f)

    assert metrics is None
