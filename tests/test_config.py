from pathlib import Path

import pytest

from linkstart.config import AppConfig, load_config, merge_channel
from linkstart.models import ChannelConfig


def test_load_minimal_config(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
channels:
  - platform: twitcasting
    channel_id: "abc"
""",
        encoding="utf-8",
    )
    app = load_config(cfg)
    assert isinstance(app, AppConfig)
    assert app.defaults.poll_interval == 60
    assert len(app.channels) == 1
    assert app.channels[0].platform == "twitcasting"
    assert app.channels[0].channel_id == "abc"


def test_merge_channel_applies_defaults():
    app = AppConfig.model_validate(
        {
            "defaults": {"poll_interval": 120, "save_dir": "/tmp/rec"},
            "channels": [{"platform": "twitcasting", "channel_id": "abc"}],
        }
    )
    merged = merge_channel(app.channels[0], app.defaults)
    assert isinstance(merged, ChannelConfig)
    assert merged.poll_interval == 120
    assert merged.save_dir == Path("/tmp/rec")


def test_merge_channel_overrides_defaults():
    app = AppConfig.model_validate(
        {
            "defaults": {"poll_interval": 120},
            "channels": [
                {
                    "platform": "twitcasting",
                    "channel_id": "abc",
                    "poll_interval": 30,
                    "cookies_from_browser": "chrome",
                    "notifier": "main",
                }
            ],
        }
    )
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.poll_interval == 30
    assert merged.cookies_from_browser == "chrome"
    assert merged.notifier_id == "main"


def test_notifier_parsed():
    app = AppConfig.model_validate(
        {
            "notifiers": [
                {"id": "main", "type": "discord", "webhook_url": "https://x/y"}
            ]
        }
    )
    assert app.notifiers[0].id == "main"
    assert app.notifiers[0].webhook_url == "https://x/y"


def test_summary_defaults():
    app = AppConfig.model_validate({})
    assert app.summary.enabled is False
    assert app.summary.cron == "0 9 * * *"


def test_invalid_yaml_raises(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("channels:\n  - platform: twitcasting\n", encoding="utf-8")
    # missing required field channel_id
    with pytest.raises(Exception):
        load_config(cfg)


def test_raw_channel_format_field_default_none():
    app = AppConfig.model_validate({
        "channels": [{"platform": "youtube", "channel_id": "x"}]
    })
    assert app.channels[0].format is None


def test_raw_channel_format_field_set():
    app = AppConfig.model_validate({
        "channels": [{"platform": "youtube", "channel_id": "x", "format": "137+140"}]
    })
    assert app.channels[0].format == "137+140"


def test_merge_channel_propagates_format():
    app = AppConfig.model_validate({
        "channels": [
            {"platform": "youtube", "channel_id": "x", "format": "137+140"}
        ]
    })
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.format == "137+140"


def test_save_dir_tilde_expanded_in_defaults():
    app = AppConfig.model_validate({
        "defaults": {"save_dir": "~/Downloads/LinkStart"},
        "channels": [{"platform": "twitcasting", "channel_id": "abc"}],
    })
    expected = Path.home() / "Downloads" / "LinkStart"
    assert app.defaults.save_dir == expected
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.save_dir == expected


def test_save_dir_tilde_expanded_in_channel_override():
    app = AppConfig.model_validate({
        "channels": [
            {
                "platform": "twitcasting",
                "channel_id": "abc",
                "save_dir": "~/Movies/streams",
            }
        ],
    })
    expected = Path.home() / "Movies" / "streams"
    assert app.channels[0].save_dir == expected
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.save_dir == expected


def test_alias_propagates_through_merge_channel():
    app = AppConfig.model_validate({
        "channels": [
            {"platform": "chzzk",
             "channel_id": "abcdef0123456789abcdef0123456789",
             "alias": "mychannel"},
        ],
    })
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.alias == "mychannel"
    assert merged.channel_id == "abcdef0123456789abcdef0123456789"


def test_alias_default_is_none():
    app = AppConfig.model_validate({
        "channels": [{"platform": "twitcasting", "channel_id": "somehandle"}],
    })
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.alias is None


def test_merge_channel_format_none_when_unset():
    app = AppConfig.model_validate({
        "channels": [{"platform": "youtube", "channel_id": "x"}]
    })
    merged = merge_channel(app.channels[0], app.defaults)
    assert merged.format is None
