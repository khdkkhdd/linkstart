import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


from linkstart.cli import build_parser, cmd_check, cmd_list, parse_args


def test_parser_run():
    p = build_parser()
    args = p.parse_args(["run", "--config", "x.yaml"])
    assert args.command == "run"
    assert args.config == Path("x.yaml")


def test_parser_check_requires_index():
    p = build_parser()
    args = p.parse_args(["check", "0"])
    assert args.command == "check"
    assert args.channel_idx == 0


def test_parser_config_before_subcommand():
    args = parse_args(["--config", "before.yaml", "list"])
    assert args.command == "list"
    assert args.config == Path("before.yaml")


def test_parser_config_default():
    args = parse_args(["list"])
    assert args.config == Path("config.yaml")


def test_log_level_env_override(monkeypatch, tmp_path):
    import logging
    from linkstart.cli import configure_logging
    monkeypatch.setenv("LINKSTART_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LINKSTART_LOG_FILE", str(tmp_path / "ls.log"))  # keep ~ clean
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        root.removeHandler(h)
    root.setLevel(logging.WARNING)
    try:
        configure_logging()
        assert logging.getLogger().level == logging.DEBUG
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in saved:
            root.addHandler(h)


def test_configure_logging_writes_to_rotating_file(monkeypatch, tmp_path):
    """Logging must land in a file automatically (independent of how the daemon
    is launched), via a size-bounded RotatingFileHandler whose path is
    configurable with LINKSTART_LOG_FILE."""
    import logging
    from logging.handlers import RotatingFileHandler
    from linkstart.cli import configure_logging

    log_file = tmp_path / "sub" / "linkstart.log"  # parent dir must be created
    monkeypatch.setenv("LINKSTART_LOG_FILE", str(log_file))
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        root.removeHandler(h)
    try:
        configure_logging()
        assert any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        logging.getLogger("linkstart.test").warning("hello-file-log")
        for h in root.handlers:
            h.flush()
        assert log_file.exists()
        assert "hello-file-log" in log_file.read_text()
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in saved:
            root.addHandler(h)


def test_configure_logging_closes_old_file_handler_on_reinvoke(monkeypatch, tmp_path):
    """Re-invoking configure_logging must close the previous RotatingFileHandler
    (release its fd) and not accumulate handlers — otherwise a re-config leaks a
    file descriptor each time."""
    import logging
    from logging.handlers import RotatingFileHandler
    from linkstart.cli import configure_logging

    monkeypatch.setenv("LINKSTART_LOG_FILE", str(tmp_path / "ls.log"))
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        root.removeHandler(h)
    try:
        configure_logging()
        first = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(first) == 1
        first_handler = first[0]

        configure_logging()  # re-invoke
        fhs = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(fhs) == 1                 # no accumulation
        assert first_handler.stream is None  # previous handler was closed (fd freed)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in saved:
            root.addHandler(h)


def test_configure_logging_file_disabled_with_empty_env(monkeypatch):
    """LINKSTART_LOG_FILE='' disables file logging (console only)."""
    import logging
    from logging.handlers import RotatingFileHandler
    from linkstart.cli import configure_logging

    monkeypatch.setenv("LINKSTART_LOG_FILE", "")
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        root.removeHandler(h)
    try:
        configure_logging()
        assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in saved:
            root.addHandler(h)


def test_configure_diagnostics_enables_faulthandler():
    """faulthandler must be enabled so a crash/stall dumps a traceback, and
    SIGUSR1 registered so a wedged daemon can be inspected on demand
    (`kill -USR1 <pid>`) without attaching a debugger."""
    import faulthandler
    import signal
    from linkstart.observability import configure_diagnostics

    configure_diagnostics()
    assert faulthandler.is_enabled()
    if hasattr(signal, "SIGUSR1"):
        # register() installs a C-level handler (invisible to signal.getsignal);
        # unregister() returns True only if one was registered. Re-register so
        # diagnostics stay on for the rest of the process.
        assert faulthandler.unregister(signal.SIGUSR1) is True
        faulthandler.register(signal.SIGUSR1, all_threads=True)


async def test_cmd_list_prints_channels(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
channels:
  - platform: twitcasting
    channel_id: "abc"
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg

    with patch("linkstart.cli.StateStore") as MockState:
        MockState.return_value.get_entry = lambda *args, **kwargs: None
        rc = await cmd_list(Args())

    captured = capsys.readouterr()
    assert "twitcasting/abc" in captured.out
    assert rc == 0


async def test_cmd_check_index_out_of_range(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
channels:
  - platform: twitcasting
    channel_id: "abc"
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg
        channel_idx = 5

    rc = await cmd_check(Args())
    captured = capsys.readouterr()
    assert "out of range" in captured.err
    assert rc == 1


async def test_cmd_check_unknown_platform_returns_1(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
channels:
  - platform: nonexistent
    channel_id: "x"
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg
        channel_idx = 0

    rc = await cmd_check(Args())
    captured = capsys.readouterr()
    assert "unknown platform" in captured.err
    assert rc == 1


async def test_cmd_check_offline_prints_offline(tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
channels:
  - platform: twitcasting
    channel_id: "abc"
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg
        channel_idx = 0

    # Patch TwitcastingPlatform.check_live to return None.
    with patch(
        "linkstart.cli.TwitcastingPlatform.check_live",
        new=AsyncMock(return_value=None),
    ):
        rc = await cmd_check(Args())

    captured = capsys.readouterr()
    assert "offline" in captured.out
    assert rc == 0


async def test_cmd_check_live_prints_live_info(tmp_path, capsys):
    from linkstart.models import LiveInfo

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
channels:
  - platform: twitcasting
    channel_id: "abc"
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg
        channel_idx = 0

    live = LiveInfo(live_id="987", title="hello", url="https://x/abc")
    with patch(
        "linkstart.cli.TwitcastingPlatform.check_live",
        new=AsyncMock(return_value=live),
    ):
        rc = await cmd_check(Args())

    captured = capsys.readouterr()
    assert "987" in captured.out
    assert "hello" in captured.out
    assert rc == 0


async def test_cmd_run_lifecycle_no_summary(tmp_path):
    """cmd_run wires Orchestrator, registers signal handlers, awaits run(),
    and in the finally block closes platforms and notifiers."""
    from linkstart.cli import cmd_run

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
notifiers:
  - id: main
    type: discord
    webhook_url: https://discord.com/api/webhooks/x/y
channels:
  - platform: twitcasting
    channel_id: "abc"
    notifier: main
summary:
  enabled: false
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg

    closed_platforms: list = []
    closed_notifiers: list = []
    signal_handlers: list = []

    # Stub Orchestrator: run() returns immediately.
    fake_orch = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch._stop = asyncio.Event()

    # Wrap close() to capture calls.


    async def mock_disc_close(self):
        closed_notifiers.append(self)

    async def mock_plat_close(self):
        closed_platforms.append(type(self).__name__)

    # Capture signal-handler registrations (don't actually register).
    real_get_loop = asyncio.get_running_loop

    class _StubLoop:
        def __init__(self, real):
            self._real = real

        def add_signal_handler(self, sig, cb):
            signal_handlers.append(sig)

        def __getattr__(self, attr):
            return getattr(self._real, attr)

    def fake_get_loop():
        return _StubLoop(real_get_loop())

    with patch("linkstart.cli.Orchestrator", return_value=fake_orch):
        with patch("linkstart.cli.StateStore"):
            with patch.object(asyncio, "get_running_loop", fake_get_loop):
                with patch(
                    "linkstart.notifier.discord.DiscordNotifier.close",
                    new=mock_disc_close,
                ):
                    # Each concrete platform's close() is mocked so we observe
                    # all four (TwitcastingPlatform and ChzzkPlatform have their
                    # own close() overrides, so patching the ABC isn't enough).
                    with patch.object(
                        __import__("linkstart.platforms.twitcasting", fromlist=["TwitcastingPlatform"]).TwitcastingPlatform,
                        "close", new=mock_plat_close,
                    ), patch.object(
                        __import__("linkstart.platforms.chzzk", fromlist=["ChzzkPlatform"]).ChzzkPlatform,
                        "close", new=mock_plat_close,
                    ), patch.object(
                        __import__("linkstart.platforms.youtube", fromlist=["YoutubePlatform"]).YoutubePlatform,
                        "close", new=mock_plat_close,
                    ):
                        rc = await cmd_run(Args())

    assert rc == 0
    fake_orch.run.assert_awaited_once()
    import signal as _signal
    assert _signal.SIGINT in signal_handlers
    assert _signal.SIGTERM in signal_handlers
    # All registered platforms closed.
    assert len(closed_platforms) == 3
    # The single Discord notifier closed.
    assert len(closed_notifiers) == 1


async def test_cmd_run_signal_handler_invokes_orch_stop(tmp_path, capsys):
    """Capture the signal handler registered by cmd_run, invoke it directly,
    and verify orch.stop() is called with the friendly stdout message."""
    from linkstart.cli import cmd_run

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
notifiers:
  - id: main
    type: discord
    webhook_url: https://discord.com/api/webhooks/x/y
channels:
  - platform: twitcasting
    channel_id: "abc"
    notifier: main
summary:
  enabled: false
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg

    fake_orch = AsyncMock()
    fake_orch.stop = MagicMock()
    fake_orch._stop = asyncio.Event()
    captured_handlers: list = []

    async def invoke_handler_then_return():
        # Wait a tick for cmd_run to install the handler, then invoke it.
        for _ in range(20):
            await asyncio.sleep(0)
            if captured_handlers:
                break
        # First invocation → "Stop requested" message + orch.stop().
        captured_handlers[0]()

    fake_orch.run = AsyncMock(side_effect=invoke_handler_then_return)

    real_get_loop = asyncio.get_running_loop

    class _CapturingLoop:
        def __init__(self, real):
            self._real = real

        def add_signal_handler(self, sig, cb):
            captured_handlers.append(cb)

        def __getattr__(self, attr):
            return getattr(self._real, attr)

    def fake_get_loop():
        return _CapturingLoop(real_get_loop())

    with patch("linkstart.cli.Orchestrator", return_value=fake_orch):
        with patch("linkstart.cli.StateStore"):
            with patch.object(asyncio, "get_running_loop", fake_get_loop):
                with patch(
                    "linkstart.notifier.discord.DiscordNotifier.close",
                    new=AsyncMock(),
                ):
                    # capsys replaces sys.stderr with a fileno-less buffer, which
                    # faulthandler.enable() in configure_diagnostics rejects; this
                    # test isn't about diagnostics, so stub it out.
                    with patch("linkstart.cli.configure_diagnostics"):
                        rc = await cmd_run(Args())

    assert rc == 0
    # Both SIGINT and SIGTERM handlers registered (same callback).
    assert len(captured_handlers) == 2
    fake_orch.stop.assert_called_once()
    captured = capsys.readouterr()
    assert "Stop requested" in captured.out


async def test_cmd_run_creates_summary_task_when_enabled(tmp_path):
    from linkstart.cli import cmd_run

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
notifiers:
  - id: main
    type: discord
    webhook_url: https://discord.com/api/webhooks/x/y
channels:
  - platform: twitcasting
    channel_id: "abc"
    notifier: main
summary:
  enabled: true
  cron: "0 9 * * *"
  notifier: main
""",
        encoding="utf-8",
    )

    class Args:
        config = cfg

    fake_orch = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch._stop = asyncio.Event()

    summary_task_holder: dict = {}
    original_create_task = asyncio.create_task

    def capturing_create_task(coro, *a, **kw):
        t = original_create_task(coro, *a, **kw)
        # The summary task is created from run_summary_loop. Capture the first
        # task created via this call site.
        summary_task_holder.setdefault("task", t)
        return t

    async def fake_run_summary(*a, **kw):
        # Return immediately so the captured task completes promptly.
        return None

    real_get_loop = asyncio.get_running_loop

    class _StubLoop:
        def __init__(self, real):
            self._real = real

        def add_signal_handler(self, sig, cb):
            pass

        def __getattr__(self, attr):
            return getattr(self._real, attr)

    def fake_get_loop():
        return _StubLoop(real_get_loop())

    with patch("linkstart.cli.Orchestrator", return_value=fake_orch):
        with patch("linkstart.cli.StateStore"):
            with patch("linkstart.cli.run_summary_loop", new=fake_run_summary):
                with patch.object(asyncio, "get_running_loop", fake_get_loop):
                    with patch.object(asyncio, "create_task", capturing_create_task):
                        with patch(
                            "linkstart.notifier.discord.DiscordNotifier.close",
                            new=AsyncMock(),
                        ):
                            rc = await cmd_run(Args())

    assert rc == 0
    # The summary task was created.
    assert "task" in summary_task_holder


def test_main_dispatches_to_subcommands(monkeypatch):
    from linkstart import cli

    # Avoid double basicConfig side effects from earlier tests.
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    # run
    monkeypatch.setattr("sys.argv", ["linkstart", "run"])
    with patch("linkstart.cli.cmd_run", new=AsyncMock(return_value=0)) as m_run:
        rc = cli.main()
    assert rc == 0
    m_run.assert_called_once()

    # list
    monkeypatch.setattr("sys.argv", ["linkstart", "list"])
    with patch("linkstart.cli.cmd_list", new=AsyncMock(return_value=0)) as m_list:
        rc = cli.main()
    assert rc == 0
    m_list.assert_called_once()

    # check
    monkeypatch.setattr("sys.argv", ["linkstart", "check", "0"])
    with patch("linkstart.cli.cmd_check", new=AsyncMock(return_value=0)) as m_check:
        rc = cli.main()
    assert rc == 0
    m_check.assert_called_once()
