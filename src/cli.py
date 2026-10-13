"""CLI argument parsing and entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import signal
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

from src import __version__
from src.color_formatter import ColorFormatter
from src.config import load_config
from src.swapper import Swapper

logger = logging.getLogger(__name__)

SERVER_SHUTDOWN_TIMEOUT = 2.0


_DURATION_RE = re.compile(r"^([0-9]+)([smh]?)$")
_DURATION_MULTIPLIERS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(value: str) -> int:
    """Parse a CLI duration into seconds.

    Accepts bare integer seconds or integer s/m/h suffixes.
    """
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError(
            "duration must be a non-negative integer with optional "
            "s, m, or h suffix"
        )
    amount, unit = match.groups()
    return int(amount) * _DURATION_MULTIPLIERS[unit]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="model-switcher",
        description="Automatic model swapping reverse proxy",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Listen port (default: 8080)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help=(
            "For positive values: total swap/non-stream timeout and "
            "stream connection/read-inactivity timeout in seconds; "
            "streams have no total timeout (default: 60)"
        ),
    )
    parser.add_argument(
        "--idle-timeout",
        type=parse_duration,
        default=parse_duration("30m"),
        metavar="DURATION",
        help="Unload idle model after duration (default: 30m; 0 disables)",
    )
    parser.add_argument(
        "--quick-switch",
        action="store_true",
        default=False,
        help=(
            "Let a different-model request supersede a model that is still "
            "loading (default: off)"
        ),
    )
    parser.add_argument(
        "--timestamp",
        action="store_true",
        default=False,
        help="Include timestamps in log output",
    )
    return parser.parse_args(argv)


async def _run_server(
    host: str,
    port: int,
    swapper: Swapper,
    shutdown_event: asyncio.Event,
) -> None:
    """Run the aiohttp server."""
    app = web.Application(client_max_size=100 * 1024**2)
    app.router.add_route("*", "/{path:.*}", swapper.handle_request)

    runner = web.AppRunner(app, shutdown_timeout=SERVER_SHUTDOWN_TIMEOUT)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("Listening on %s:%d", host, port)
    logger.info(
        "Available models: %s",
        ", ".join(swapper.config.public_names()),
    )

    await shutdown_event.wait()

    await runner.cleanup()
    logger.info("Server stopped")


async def _main(argv: list[str] | None = None) -> None:
    """Async main entry point."""
    args = parse_args(argv)

    # Setup logging
    handler = logging.StreamHandler()
    handler.setFormatter(
        ColorFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            show_timestamp=args.timestamp,
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    # Load config
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    logger.info(
        "Loaded %d model(s): %s",
        len(config.models),
        list(config.models.keys()),
    )

    # Create shared client session for upstream requests
    client_timeout = aiohttp.ClientTimeout(total=args.timeout)
    client = aiohttp.ClientSession(timeout=client_timeout)

    # Create swapper
    swapper = Swapper(
        config,
        timeout=args.timeout,
        idle_timeout=args.idle_timeout,
        quick_switch=args.quick_switch,
        client=client,
    )
    # Kept for control API config reload (absolute, so a later chdir
    # cannot change which file is read).
    swapper.config_path = Path(args.config).resolve()

    # Shutdown coordination
    shutdown_event = asyncio.Event()
    main_task = asyncio.current_task()
    shutdown_requested = False

    def _signal_handler() -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            logger.info("Shutdown signal received again; forcing shutdown")
            if main_task is not None:
                main_task.cancel()
            return

        logger.info("Shutdown signal received")
        shutdown_requested = True
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        await _run_server(args.host, args.port, swapper, shutdown_event)
    finally:
        await swapper.shutdown()


def main() -> None:
    """Sync entry point for console script."""
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
