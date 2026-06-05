from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

from linkstart.models import ChannelConfig, Event, EventType, LiveInfo
from linkstart.notifier.discord import (
    DiscordNotifier,
    build_embed,
    _format_duration,
    _format_size,
)


@pytest.fixture
def channel():
    return ChannelConfig(platform="twitcasting", channel_id="abc")


def test_format_size():
    assert _format_size(None) == "0 B"
    assert _format_size(0) == "0 B"
    assert _format_size(512) == "512.0 B"
    assert _format_size(1500) == "1.5 KB"
    assert _format_size(2 * 1024 * 1024) == "2.0 MB"
    assert _format_size(1024 ** 5 * 3).endswith("PB")


def test_format_duration():
    assert _format_duration(None) == "0s"
    assert _format_duration(0) == "0s"
    assert _format_duration(45) == "45s"
    assert _format_duration(75) == "1m 15s"
    assert _format_duration(3725) == "1h 2m"


def test_embed_live_started(channel):
    live = LiveInfo(
        live_id="1",
        title="hello",
        url="https://x",
        thumbnail_url="https://t",
    )
    e = Event(type=EventType.LIVE_STARTED, channel=channel, live=live)
    embed = build_embed(e)
    assert "is live" in embed["title"]
    assert embed["description"] == "hello"
    assert embed["thumbnail"]["url"] == "https://t"


def test_embed_download_started(channel):
    e = Event(type=EventType.DOWNLOAD_STARTED, channel=channel)
    embed = build_embed(e)
    assert "Recording started" in embed["title"]
    assert channel.channel_id in embed["description"]


def test_embed_summary(channel):
    e = Event(type=EventType.SUMMARY, channel=channel, message="2 recordings, 50 MB")
    embed = build_embed(e)
    assert "summary" in embed["title"].lower()
    assert embed["description"] == "2 recordings, 50 MB"


def test_embed_download_finished(channel):
    e = Event(
        type=EventType.DOWNLOAD_FINISHED,
        channel=channel,
        file_path=Path("/tmp/x.mp4"),
        size_bytes=1024,
        duration_sec=120,
        retry_count=2,
    )
    embed = build_embed(e)
    assert "finished" in embed["title"].lower()
    field_names = {f["name"] for f in embed["fields"]}
    assert {"file", "size", "duration", "retries"} == field_names


def test_embed_error(channel):
    e = Event(type=EventType.ERROR, channel=channel, message="kaboom")
    embed = build_embed(e)
    assert "Error" in embed["title"]
    assert embed["description"] == "kaboom"


def test_embed_error_surfaces_file_path_when_present(channel):
    """When an ERROR event carries a file_path (e.g. validation-invalid case
    where a misleading file is on disk), the embed must show the path as a
    distinct field so the operator can find it without parsing the message."""
    e = Event(
        type=EventType.ERROR,
        channel=channel,
        message="likely login wall: 52 kbps over 4278s [file: /tmp/junk.mp4]",
        file_path=Path("/tmp/junk.mp4"),
    )
    embed = build_embed(e)
    assert "Error" in embed["title"]
    field_names = {f["name"] for f in embed.get("fields", [])}
    assert "file" in field_names
    file_field = next(f for f in embed["fields"] if f["name"] == "file")
    assert "/tmp/junk.mp4" in file_field["value"]


def test_embed_unknown_type_raises(channel):
    e = Event(type=EventType.LIVE_STARTED, channel=channel)
    e.type = "weird"  # type: ignore[assignment]
    with pytest.raises(ValueError):
        build_embed(e)


async def test_notifier_posts_to_webhook(channel):
    e = Event(type=EventType.ERROR, channel=channel, message="x")
    url = "https://discord.com/api/webhooks/123/abc"
    with aioresponses() as m:
        m.post(url, status=204)
        notifier = DiscordNotifier(url)
        try:
            await notifier.notify(e)
        finally:
            await notifier.close()
        # Verify request was made
        assert any(
            k[0] == "POST" and str(k[1]).startswith(url) for k in m.requests.keys()
        )


async def test_notifier_swallows_http_errors(channel):
    e = Event(type=EventType.ERROR, channel=channel, message="x")
    url = "https://discord.com/api/webhooks/123/abc"
    with aioresponses() as m:
        m.post(url, status=500)
        notifier = DiscordNotifier(url)
        try:
            await notifier.notify(e)  # must not raise
        finally:
            await notifier.close()


async def test_notifier_swallows_network_errors(channel):
    """aiohttp.ClientError during POST must be swallowed (no propagation)."""
    e = Event(type=EventType.ERROR, channel=channel, message="x")
    url = "https://discord.com/api/webhooks/123/abc"
    with aioresponses() as m:
        m.post(url, exception=aiohttp.ClientError("connection reset"))
        notifier = DiscordNotifier(url)
        try:
            await notifier.notify(e)  # must not raise
        finally:
            await notifier.close()


async def test_notifier_swallows_build_embed_errors(channel):
    e = Event(type=EventType.LIVE_STARTED, channel=channel)
    e.type = "totally-unknown-type"  # type: ignore[assignment]
    url = "https://discord.com/api/webhooks/123/abc"
    # No aioresponses needed — build_embed will raise before any HTTP
    notifier = DiscordNotifier(url)
    try:
        await notifier.notify(e)  # must not raise
    finally:
        await notifier.close()


def test_embed_download_interrupted(channel):
    e = Event(
        type=EventType.DOWNLOAD_INTERRUPTED,
        channel=channel,
        message="loop: edge   attempt: 2",
        retry_count=2,
    )
    embed = build_embed(e)
    assert "interrupted" in embed["title"].lower()
    assert "edge" in str(embed)
    # Yellow color (warn)
    assert "color" in embed


def test_embed_download_finished_includes_extra_files(channel):
    e = Event(
        type=EventType.DOWNLOAD_FINISHED,
        channel=channel,
        file_path=Path("/tmp/base.mp4"),
        extra_files=[Path("/tmp/x.edge_001.mp4"), Path("/tmp/x.edge_002.mp4")],
        size_bytes=1024,
        duration_sec=60,
        retry_count=0,
    )
    embed = build_embed(e)
    field_names = {f["name"] for f in embed["fields"]}
    assert "extra files" in field_names


def test_embed_download_finished_omits_extras_when_empty(channel):
    e = Event(
        type=EventType.DOWNLOAD_FINISHED,
        channel=channel,
        file_path=Path("/tmp/base.mp4"),
        extra_files=[],
        size_bytes=1024,
        duration_sec=60,
        retry_count=0,
    )
    embed = build_embed(e)
    field_names = {f["name"] for f in embed["fields"]}
    assert "extra files" not in field_names
