import pytest


@pytest.fixture
def tmp_state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture(autouse=True)
def _fast_downloader_retry_delay(monkeypatch):
    """Default delay between is_still_live retries is 5s in production. Skip it
    in tests so existing 'broadcast ended → finalize' paths don't accrue 10s
    of real time per test."""
    monkeypatch.setattr(
        "linkstart.downloader._loop._DownloaderBase.IS_STILL_LIVE_RETRY_DELAY",
        0,
    )
