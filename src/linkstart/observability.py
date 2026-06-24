"""Daemon-lifecycle observability: on-demand stack dumps for a wedged daemon."""
import signal


def configure_diagnostics() -> None:
    """Enable faulthandler + SIGUSR1 thread-dump for diagnosing a wedged daemon."""
    import faulthandler
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
