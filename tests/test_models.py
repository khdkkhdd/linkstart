from pathlib import Path

import pytest

from linkstart.models import (
    ChannelConfig,
    DownloadResult,
    Event,
    EventType,
    LiveInfo,
    ValidationResult,
)


def test_live_info_is_frozen():
    info = LiveInfo(live_id="1", title="t", url="https://x")
    with pytest.raises(Exception):
        info.title = "new"  # type: ignore[misc]


def test_live_info_optional_fields_default_none():
    info = LiveInfo(live_id="1", title="t", url="https://x")
    assert info.started_at is None
    assert info.thumbnail_url is None


def test_channel_config_defaults():
    c = ChannelConfig(platform="twitcasting", channel_id="abc")
    assert c.poll_interval == 60
    assert c.save_dir == Path("recordings")
    assert c.cookies_from_browser is None
    assert c.notifier_id is None


def test_event_type_string_values():
    assert EventType.LIVE_STARTED == "live_started"
    assert EventType.DOWNLOAD_STARTED == "download_started"
    assert EventType.DOWNLOAD_INTERRUPTED == "download_interrupted"
    assert EventType.DOWNLOAD_FINISHED == "download_finished"
    assert EventType.ERROR == "error"
    assert EventType.SUMMARY == "summary"


def test_download_result_defaults():
    r = DownloadResult(success=False)
    assert r.file_path is None
    assert r.error is None
    assert r.size_bytes == 0
    assert r.duration_sec == 0
    assert r.retry_count == 0


def test_event_minimal_construction():
    ch = ChannelConfig(platform="twitcasting", channel_id="abc")
    e = Event(type=EventType.LIVE_STARTED, channel=ch)
    assert e.live is None
    assert e.retry_count == 0


def test_event_type_download_interrupted():
    assert EventType.DOWNLOAD_INTERRUPTED == "download_interrupted"


def test_event_extra_files_default_empty():
    ch = ChannelConfig(platform="twitcasting", channel_id="abc")
    e = Event(type=EventType.LIVE_STARTED, channel=ch)
    assert e.extra_files == []


def test_download_result_extra_files_default_empty():
    r = DownloadResult(success=False)
    assert r.extra_files == []


def test_download_result_extra_files_isolated_between_instances():
    a = DownloadResult(success=True)
    b = DownloadResult(success=True)
    a.extra_files.append(Path("/tmp/x"))
    assert b.extra_files == []


def test_channel_config_format_default_none():
    c = ChannelConfig(platform="twitcasting", channel_id="abc")
    assert c.format is None


def test_channel_config_format_set():
    c = ChannelConfig(platform="youtube", channel_id="x", format="299+140")
    assert c.format == "299+140"


def test_validation_result_defaults():
    v = ValidationResult(status="ok")
    assert v.status == "ok"
    assert v.reason is None
    assert v.metrics == {}


def test_validation_result_invalid_with_reason_and_metrics():
    v = ValidationResult(
        status="invalid",
        reason="likely login wall",
        metrics={"bitrate_kbps": 52.0, "duration_s": 4278.0},
    )
    assert v.status == "invalid"
    assert v.reason == "likely login wall"
    assert v.metrics["bitrate_kbps"] == 52.0


def test_validation_result_is_frozen():
    v = ValidationResult(status="ok")
    with pytest.raises(Exception):
        v.status = "invalid"  # type: ignore[misc]


def test_validation_result_metrics_isolated_between_instances():
    a = ValidationResult(status="ok")
    b = ValidationResult(status="ok")
    a.metrics["x"] = 1.0
    assert b.metrics == {}


def test_download_result_validation_default_ok():
    r = DownloadResult(success=True)
    assert r.validation is not None
    assert r.validation.status == "ok"


def test_download_result_carries_validation():
    v = ValidationResult(status="invalid", reason="bad file")
    r = DownloadResult(success=True, validation=v)
    assert r.validation.status == "invalid"
    assert r.validation.reason == "bad file"
