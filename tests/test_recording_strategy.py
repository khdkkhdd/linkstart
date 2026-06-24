"""Tests for the RecordingStrategy interface — platforms declare *how* they are
recorded (edge HLS-live vs dual from-start) by returning a strategy object,
replacing the old `if platform.supports_live_from_start:` branch in Downloader.
"""
from linkstart.downloader import Downloader
from linkstart.downloader._dual import DualRecordingStrategy
from linkstart.downloader._edge import EdgeRecordingStrategy
from linkstart.platforms.chzzk import ChzzkPlatform
from linkstart.platforms.twitcasting import TwitcastingPlatform
from linkstart.platforms.youtube import YoutubePlatform


def test_hls_live_platforms_use_edge_strategy():
    dl = Downloader()
    assert isinstance(TwitcastingPlatform().recording_strategy(dl), EdgeRecordingStrategy)
    assert isinstance(ChzzkPlatform().recording_strategy(dl), EdgeRecordingStrategy)


def test_youtube_uses_dual_strategy():
    dl = Downloader()
    assert isinstance(YoutubePlatform().recording_strategy(dl), DualRecordingStrategy)


def test_strategy_delegates_to_the_same_downloader_ctx():
    # The strategy must call back into the SAME Downloader instance so shared
    # helpers (ffmpeg remux, cleanup) and their state/patches are honored.
    dl = Downloader()
    assert TwitcastingPlatform().recording_strategy(dl).ctx is dl


def test_edge_owns_its_tuning():
    assert EdgeRecordingStrategy.EDGE_STALL_SEC == 45.0
    assert EdgeRecordingStrategy.EDGE_LOOP_SLEEP == 0.0
    assert EdgeRecordingStrategy.NO_OUTPUT_FAIL_LIMIT == 3


def test_dual_owns_its_tuning():
    assert DualRecordingStrategy.DUAL_STALL_SEC == 300.0
    assert DualRecordingStrategy.FULL_LOOP_SLEEP == 5.0
    assert DualRecordingStrategy.INTERRUPTED_DEDUP_SEC == 300.0
