import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from linkstart.summary import (
    append_recording_record,
    build_summary_text,
    load_records_since,
    next_fire_time,
)


def test_append_and_load(tmp_path, monkeypatch):
    log_path = tmp_path / "recordings.jsonl"
    monkeypatch.setattr("linkstart.summary._log_path", lambda: log_path)

    append_recording_record(
        platform="twitcasting",
        channel_id="abc",
        file_path=Path("/tmp/x.mp4"),
        size_bytes=1024,
        duration_sec=60,
    )
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["platform"] == "twitcasting"
    assert rec["channel_id"] == "abc"
    assert rec["size_bytes"] == 1024


def test_load_records_since_no_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("linkstart.summary._log_path", lambda: tmp_path / "missing.jsonl")
    assert load_records_since(datetime.now(timezone.utc)) == []


def test_load_records_since_skips_blank_and_malformed_lines(tmp_path, monkeypatch):
    log_path = tmp_path / "recordings.jsonl"
    monkeypatch.setattr("linkstart.summary._log_path", lambda: log_path)
    new_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    log_path.write_text(
        "\n".join(
            [
                "",                                                       # blank
                "{not json}",                                             # malformed JSON
                json.dumps({"platform": "z", "channel_id": "c"}),         # missing timestamp
                json.dumps({"timestamp": "not-a-date", "platform": "z"}), # malformed ts
                json.dumps({"timestamp": new_ts, "platform": "y",
                            "channel_id": "b", "size_bytes": 2,
                            "duration_sec": 2, "file_path": "/y"}),       # valid
            ]
        ),
        encoding="utf-8",
    )
    records = load_records_since(datetime.now(timezone.utc) - timedelta(hours=24))
    assert len(records) == 1
    assert records[0]["platform"] == "y"


def test_human_size_handles_large_units():
    from linkstart.summary import _human_size
    # Beyond TB → returns PB suffix.
    huge = 1024 ** 5 * 3   # 3 PB
    assert _human_size(huge).endswith("PB")
    # Mid-range values use intermediate units.
    assert _human_size(1024 * 1024 * 5).endswith("MB")


def test_load_records_since(tmp_path, monkeypatch):
    log_path = tmp_path / "recordings.jsonl"
    monkeypatch.setattr("linkstart.summary._log_path", lambda: log_path)

    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    log_path.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": old, "platform": "x", "channel_id": "a", "size_bytes": 1, "duration_sec": 1, "file_path": "/x"}),
                json.dumps({"timestamp": new, "platform": "y", "channel_id": "b", "size_bytes": 2, "duration_sec": 2, "file_path": "/y"}),
            ]
        ),
        encoding="utf-8",
    )

    since = datetime.now(timezone.utc) - timedelta(days=1)
    records = load_records_since(since)
    assert len(records) == 1
    assert records[0]["platform"] == "y"


def test_build_summary_text_empty():
    text = build_summary_text([])
    assert "No recordings" in text


def test_build_summary_text_groups_by_channel():
    records = [
        {"platform": "twitcasting", "channel_id": "abc", "size_bytes": 1000, "duration_sec": 60, "file_path": "/x", "timestamp": "t"},
        {"platform": "twitcasting", "channel_id": "abc", "size_bytes": 2000, "duration_sec": 30, "file_path": "/y", "timestamp": "t"},
        {"platform": "chzzk", "channel_id": "xyz", "size_bytes": 500, "duration_sec": 10, "file_path": "/z", "timestamp": "t"},
    ]
    text = build_summary_text(records)
    assert "twitcasting/abc" in text
    assert "2 recordings" in text
    assert "chzzk/xyz" in text


def test_next_fire_time_advances():
    from datetime import datetime as dt
    base = dt(2026, 6, 3, 8, 30)
    nxt = next_fire_time("0 9 * * *", now=base)
    assert nxt > base
    assert nxt.hour == 9 and nxt.minute == 0


@pytest.mark.asyncio
async def test_run_summary_loop_fires_then_stops(monkeypatch, tmp_path):
    import asyncio
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from linkstart.models import ChannelConfig
    from linkstart.summary import run_summary_loop

    # Force next_fire_time to fire ~immediately
    monkeypatch.setattr(
        "linkstart.summary.next_fire_time",
        lambda *_a, **_kw: datetime.now() + timedelta(milliseconds=50),
    )
    # Force load_records_since to return empty (we don't care about contents here)
    monkeypatch.setattr("linkstart.summary.load_records_since", lambda since: [])

    notifier = AsyncMock()
    notifier.notify = AsyncMock()
    sample_channel = ChannelConfig(platform="(summary)", channel_id="-")
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_summary_loop("* * * * *", notifier, sample_channel, stop)
    )
    # Let the loop fire once
    await asyncio.sleep(0.2)
    # Stop the loop
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    # At least one summary was sent
    assert notifier.notify.await_count >= 1
    # The sent event was a SUMMARY
    args, _kwargs = notifier.notify.call_args
    fired_event = args[0]
    from linkstart.models import EventType
    assert fired_event.type == EventType.SUMMARY


@pytest.mark.asyncio
async def test_run_summary_loop_swallows_notifier_exception(monkeypatch):
    import asyncio
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from linkstart.models import ChannelConfig
    from linkstart.summary import run_summary_loop

    monkeypatch.setattr(
        "linkstart.summary.next_fire_time",
        lambda *_a, **_kw: datetime.now() + timedelta(milliseconds=50),
    )
    monkeypatch.setattr("linkstart.summary.load_records_since", lambda since: [])

    notifier = AsyncMock()
    notifier.notify = AsyncMock(side_effect=RuntimeError("webhook down"))
    sample_channel = ChannelConfig(platform="(summary)", channel_id="-")
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_summary_loop("* * * * *", notifier, sample_channel, stop)
    )
    await asyncio.sleep(0.2)
    stop.set()
    # Loop must not have crashed despite the notifier exception.
    await asyncio.wait_for(task, timeout=2.0)
    assert notifier.notify.await_count >= 1
