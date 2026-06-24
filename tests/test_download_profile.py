"""Tests for DownloadProfile — structured description of how a platform's
stream is fetched, replacing opaque yt_dlp_args strings + the implicit
output-container assumption that used to live in the edge recorder."""
from linkstart.models import DownloadProfile


def test_default_is_mp4_auto_no_args():
    p = DownloadProfile()
    assert p.container == "mp4"
    assert p.downloader == "auto"
    assert p.part_suffix == ".mp4"
    assert p.to_yt_dlp_args() == []


def test_mpegts_container_maps_to_ts_suffix_and_flag():
    p = DownloadProfile(container="mpegts")
    assert p.part_suffix == ".ts"
    assert p.to_yt_dlp_args() == ["--hls-use-mpegts"]


def test_native_downloader_prefers_native():
    p = DownloadProfile(container="mpegts", downloader="native")
    args = p.to_yt_dlp_args()
    assert "--hls-use-mpegts" in args
    assert "--hls-prefer-native" in args


def test_ffmpeg_downloader_forces_ffmpeg():
    p = DownloadProfile(downloader="ffmpeg")
    args = p.to_yt_dlp_args()
    assert args[args.index("--downloader") + 1] == "ffmpeg"


def test_extra_args_appended_verbatim():
    p = DownloadProfile(container="mpegts", extra_args=("--no-part", "--foo"))
    args = p.to_yt_dlp_args()
    assert args[-2:] == ["--no-part", "--foo"]


def test_is_frozen_hashable():
    # Profiles are value objects; safe to share/compare.
    assert DownloadProfile() == DownloadProfile()
    assert DownloadProfile(container="mpegts") != DownloadProfile()
    hash(DownloadProfile())  # must not raise
