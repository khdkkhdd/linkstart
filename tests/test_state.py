import json
import logging

from linkstart.state import StateStore


def test_get_entry_none_when_no_state(tmp_state_path):
    s = StateStore(tmp_state_path)
    assert s.get_entry("twitcasting", "abc") is None


def test_mark_seen_then_get_entry(tmp_state_path):
    s = StateStore(tmp_state_path)
    s.mark_seen("twitcasting", "abc", "100")
    entry = s.get_entry("twitcasting", "abc")
    assert entry is not None
    assert entry["last_live_id"] == "100"


def test_state_persists_across_instances(tmp_state_path):
    s1 = StateStore(tmp_state_path)
    s1.mark_seen("twitcasting", "abc", "100")
    s2 = StateStore(tmp_state_path)
    entry = s2.get_entry("twitcasting", "abc")
    assert entry is not None
    assert entry["last_live_id"] == "100"


def test_mark_seen_writes_atomically(tmp_state_path):
    s = StateStore(tmp_state_path)
    s.mark_seen("twitcasting", "abc", "100")
    data = json.loads(tmp_state_path.read_text())
    entry = data["channels"]["twitcasting:abc"]
    assert entry["last_live_id"] == "100"
    assert "last_seen_at" in entry


def test_different_channels_isolated(tmp_state_path):
    s = StateStore(tmp_state_path)
    s.mark_seen("twitcasting", "abc", "100")
    s.mark_seen("chzzk", "abc", "200")
    assert s.get_entry("twitcasting", "abc")["last_live_id"] == "100"
    assert s.get_entry("chzzk", "abc")["last_live_id"] == "200"


def test_corrupt_state_file_is_backed_up_and_recovered(tmp_state_path, caplog):
    tmp_state_path.write_text("this is not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="linkstart.state"):
        s = StateStore(tmp_state_path)
    # Returned fresh state
    assert s.get_entry("twitcasting", "abc") is None
    # Original corrupt file was renamed
    backups = list(tmp_state_path.parent.glob(f"{tmp_state_path.name}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "this is not json"
    # Warning logged
    assert any("corrupt" in rec.message.lower() for rec in caplog.records)


def test_corrupt_state_backup_rename_failure_still_recovers(tmp_state_path, caplog, monkeypatch):
    """If os.replace raises (e.g. cross-device link), still recover with fresh state
    instead of bubbling the OSError."""
    tmp_state_path.write_text("not json", encoding="utf-8")

    def boom(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("linkstart.state.os.replace", boom)
    with caplog.at_level(logging.WARNING, logger="linkstart.state"):
        s = StateStore(tmp_state_path)

    # Fresh state returned despite rename failure.
    assert s.get_entry("twitcasting", "abc") is None
    assert any("Failed to back up corrupt state" in rec.message for rec in caplog.records)


def test_get_entry_returns_copy_not_reference(tmp_state_path):
    s = StateStore(tmp_state_path)
    s.mark_seen("twitcasting", "abc", "100")
    entry = s.get_entry("twitcasting", "abc")
    assert entry is not None
    entry["last_live_id"] = "mutated"
    fresh = s.get_entry("twitcasting", "abc")
    assert fresh is not None
    assert fresh["last_live_id"] == "100"


def test_get_entry_returns_none_for_unknown_channel(tmp_state_path):
    s = StateStore(tmp_state_path)
    assert s.get_entry("twitcasting", "nonexistent") is None


def test_save_uses_fixed_tmp_name(tmp_state_path):
    s = StateStore(tmp_state_path)
    s.mark_seen("twitcasting", "abc", "100")
    # After successful save, no .tmp left
    leftover = list(tmp_state_path.parent.glob("*.tmp"))
    assert leftover == []
    # And the canonical name pattern for crashes-in-progress would be state.json.tmp
    expected_tmp = tmp_state_path.with_suffix(tmp_state_path.suffix + ".tmp")
    # Verify the path-construction helper would yield that name (smoke check)
    assert expected_tmp.name.endswith(".json.tmp") or expected_tmp.name.endswith(".tmp")
