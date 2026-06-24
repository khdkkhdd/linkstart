from datetime import datetime
from pathlib import Path

from linkstart.downloader._paths import RecordingPaths, sanitize_title, unique_path
from linkstart.models import ChannelConfig, LiveInfo


def _channel(tmp_path) -> ChannelConfig:
    return ChannelConfig(
        platform="twitcasting", channel_id="c:abc",
        alias="someuser", save_dir=tmp_path,
    )


def _live() -> LiveInfo:
    return LiveInfo(live_id="live1", title="hello world", url="http://x")


def test_sanitize_title_strips_invalid_and_truncates():
    assert sanitize_title('a/b:c*?"<>|') == "a_b_c______"
    assert sanitize_title("") == "untitled"
    assert len(sanitize_title("x" * 200)) == 80


def test_unique_path_suffixes_on_collision(tmp_path):
    target = tmp_path / "a.mp4"
    assert unique_path(target) == target
    target.write_bytes(b"x")
    assert unique_path(target) == tmp_path / "a_2.mp4"


def test_make_parts_dir_layout(tmp_path):
    paths = RecordingPaths()
    d = paths.make_parts_dir(_channel(tmp_path), _live())
    date = datetime.now().strftime("%Y-%m-%d")
    assert d == tmp_path / "twitcasting" / "someuser" / f"{date}_hello world.parts"
    assert d.is_dir()


def test_final_path_layout(tmp_path):
    paths = RecordingPaths()
    f = paths.final_path(_channel(tmp_path), _live())
    date = datetime.now().strftime("%Y-%m-%d")
    assert f == tmp_path / "twitcasting" / "someuser" / f"{date}_hello world.mp4"


def test_discard_parts_dir_removes_tree(tmp_path):
    paths = RecordingPaths()
    d = tmp_path / "x.parts"
    d.mkdir()
    (d / "part01.ts").write_bytes(b"data")
    paths.discard_parts_dir(d)
    assert not d.exists()
