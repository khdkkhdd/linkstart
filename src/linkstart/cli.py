"""Command-line interface."""
import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from linkstart.config import load_config, merge_channel
from linkstart.downloader import Downloader
from linkstart.notifier.discord import DiscordNotifier
from linkstart.observability import configure_diagnostics
from linkstart.orchestrator import Orchestrator
from linkstart.platforms.chzzk import ChzzkPlatform
from linkstart.platforms.cime import CimePlatform
from linkstart.platforms.twitcasting import TwitcastingPlatform
from linkstart.platforms.youtube import YoutubePlatform
from linkstart.state import StateStore
from linkstart.summary import run_summary_loop


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(prog="linkstart", parents=[common])
    sub = p.add_subparsers(dest="command", required=True)
    viewer_options = argparse.ArgumentParser(add_help=False)
    viewer_options.add_argument("--host", default=argparse.SUPPRESS, help="Viewer bind address (default: 127.0.0.1)")
    viewer_options.add_argument("--port", type=int, default=argparse.SUPPRESS, help="Viewer port (default: 8788)")
    run = sub.add_parser("run", parents=[common, viewer_options], help="Run the recorder daemon")
    run.add_argument("--serve", action="store_true", help="Also run the recording viewer (same as serve)")
    sub.add_parser(
        "list",
        parents=[common],
        help="List configured channels and last-seen state",
    )
    check = sub.add_parser(
        "check",
        parents=[common],
        help="Run one live-state check for a configured channel (debugging)",
    )
    check.add_argument("channel_idx", type=int)
    sub.add_parser("serve", parents=[common, viewer_options], help="Browse and play recordings")
    return p


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run" and not args.serve and (hasattr(args, "host") or hasattr(args, "port")):
        parser.error("run: --host and --port require --serve")
    if args.command in ("run", "serve"):
        if not hasattr(args, "host"):
            args.host = "127.0.0.1"
        if not hasattr(args, "port"):
            args.port = 8788
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
    if not hasattr(args, "config"):
        args.config = Path("config.yaml")
    return args


def _resolve_log_file() -> Path | None:
    """Return the log file path from ``LINKSTART_LOG_FILE``, or the default XDG state dir; empty env value disables file logging."""
    import os
    env = os.environ.get("LINKSTART_LOG_FILE")
    if env is not None:
        return Path(env).expanduser() if env.strip() else None
    return Path.home() / ".local" / "state" / "linkstart" / "linkstart.log"


def configure_logging() -> None:
    """Configure console + rotating-file logging."""
    import os
    from logging.handlers import RotatingFileHandler
    level = os.environ.get("LINKSTART_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    # Reset to a clean slate; close removed handlers to release file descriptors.
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    log_path = _resolve_log_file()
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
            logging.getLogger(__name__).info("logging to %s", log_path)
        except OSError as e:
            logging.getLogger(__name__).warning(
                "could not open log file %s: %s (console logging only)", log_path, e
            )


def _build_platforms() -> dict:
    return {
        "twitcasting": TwitcastingPlatform(),
        "chzzk": ChzzkPlatform(),
        "cime": CimePlatform(),
        "youtube": YoutubePlatform(),
    }


async def cmd_run(args) -> int:
    configure_diagnostics()
    config = load_config(args.config)
    platforms = _build_platforms()
    notifiers = {
        n.id: DiscordNotifier(n.webhook_url)
        for n in config.notifiers
        if n.type == "discord"
    }
    state = StateStore()
    orch = Orchestrator(
        config=config,
        platforms=platforms,
        notifiers=notifiers,
        downloader=Downloader(),
        state=state,
    )

    loop = asyncio.get_running_loop()
    interrupt_count = {"n": 0}

    def handle_sig():
        interrupt_count["n"] += 1
        if interrupt_count["n"] == 1:
            print(
                "Stop requested — terminating yt-dlp gracefully, saving what's "
                "recorded, then exiting (Ctrl-C again to force exit without "
                "cleanup).",
                flush=True,
            )
            orch.stop()
        else:
            print("Force-exiting (skipping cleanup).", flush=True)
            import os
            os._exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_sig)

    summary_task = None
    if config.summary.enabled and config.summary.notifier in notifiers:
        from linkstart.models import ChannelConfig
        placeholder = ChannelConfig(platform="(summary)", channel_id="-")
        summary_task = asyncio.create_task(
            run_summary_loop(
                config.summary.cron,
                notifiers[config.summary.notifier],
                placeholder,
                orch._stop,
            )
        )

    viewer_runner = None
    try:
        if getattr(args, "serve", False):
            from aiohttp import web
            from linkstart.viewer import create_app
            viewer_runner = web.AppRunner(create_app(config))
            try:
                await viewer_runner.setup()
                await web.TCPSite(viewer_runner, args.host, args.port).start()
            except OSError as exc:
                print(f"Could not start viewer on {args.host}:{args.port}: {exc}", file=sys.stderr)
                return 1
            logging.getLogger(__name__).info("recording viewer at http://%s:%s", args.host, args.port)
        await orch.run()
    finally:
        if viewer_runner is not None:
            await viewer_runner.cleanup()
        if summary_task is not None:
            summary_task.cancel()
        for p in platforms.values():
            await p.close()
        for n in notifiers.values():
            await n.close()
    return 0


async def cmd_list(args) -> int:
    config = load_config(args.config)
    state = StateStore()
    for raw in config.channels:
        ch = merge_channel(raw, config.defaults)
        entry = state.get_entry(ch.platform, ch.channel_id) or {}
        print(
            f"- {ch.platform}/{ch.channel_id} → "
            f"last live: {entry.get('last_live_id', '-')} "
            f"at {entry.get('last_seen_at', '-')}"
        )
    return 0


async def cmd_check(args) -> int:
    config = load_config(args.config)
    if not (0 <= args.channel_idx < len(config.channels)):
        print("channel index out of range", file=sys.stderr)
        return 1
    raw = config.channels[args.channel_idx]
    ch = merge_channel(raw, config.defaults)
    platforms = _build_platforms()
    plat = platforms.get(ch.platform)
    if plat is None:
        print(f"unknown platform: {ch.platform}", file=sys.stderr)
        return 1
    try:
        live = await plat.check_live(ch)
        if live:
            print(f"LIVE: id={live.live_id}, title={live.title}, url={live.url}")
        else:
            print("offline")
    finally:
        for p in platforms.values():
            await p.close()
    return 0


def main() -> int:
    configure_logging()
    args = parse_args()
    if args.command == "serve":
        from aiohttp import web
        from linkstart.viewer import create_app
        web.run_app(create_app(load_config(args.config)), host=args.host, port=args.port)
        return 0
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "list":
        return asyncio.run(cmd_list(args))
    if args.command == "check":
        return asyncio.run(cmd_check(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
