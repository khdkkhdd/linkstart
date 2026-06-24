from linkstart.downloader import Downloader
from linkstart.downloader._context import RecordingContext


def test_downloader_satisfies_recording_context():
    d = Downloader()
    ctx: RecordingContext = d  # structural typing smoke check
    assert hasattr(ctx, "paths") and hasattr(ctx, "process") and hasattr(ctx, "media")
    assert hasattr(ctx, "attempt_loop") and hasattr(ctx, "run_attempt")
