"""Entry point for grbl-proxy.

Wires together config, serial connection, and TCP server. Handles SIGTERM for
clean systemd shutdown. Invoked via the `grbl-proxy` console script.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from grbl_proxy.config import load_config, resolve_serial_port
from grbl_proxy.serial_conn import SerialConnection, SerialDisconnectedError
from grbl_proxy.tcp_server import TcpServer

logger = logging.getLogger(__name__)

GRBL_INIT_DELAY = 2.0  # seconds to wait after serial open before sending commands


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Quieten noisy libraries
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def _main(config_path: Path | None = None, debug: bool = False) -> None:
    _setup_logging(debug)

    config = load_config(config_path)
    port = resolve_serial_port(config.serial)

    logger.info("Starting grbl-proxy")
    logger.info("  Serial port : %s @ %d baud", port, config.serial.baud)
    logger.info("  TCP server  : %s:%d", config.tcp.host, config.tcp.port)

    serial_conn = SerialConnection(config.serial, port=port)

    # Attempt initial serial connection (non-fatal — reconnect loop will retry)
    try:
        await serial_conn.connect()
        logger.info("Serial port opened: %s", port)
        # Brief delay so GRBL can finish initialising before we send anything
        await asyncio.sleep(GRBL_INIT_DELAY)
    except SerialDisconnectedError as e:
        logger.warning("Could not open serial port on startup: %s", e)
        logger.warning("Reconnect loop will keep retrying every %gs", config.serial.reconnect_interval)

    # Background task: keep serial reconnected if USB is unplugged/replugged
    reconnect_task = asyncio.create_task(
        serial_conn.run_reconnect_loop(), name="serial-reconnect"
    )

    tcp_server = TcpServer(config.tcp.host, config.tcp.port, serial_conn)
    server = await tcp_server.start()

    # Graceful shutdown on SIGTERM (systemd) or SIGINT (Ctrl-C)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        logger.info("Received %s, shutting down...", sig_name)
        stop_event.set()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        loop.add_signal_handler(sig, _request_stop, name)

    logger.info("grbl-proxy running. Press Ctrl-C to stop.")

    await stop_event.wait()

    # Cancel relay tasks and close TCP connections before waiting on the server,
    # otherwise server.wait_closed() blocks on the still-running handler coroutines.
    reconnect_task.cancel()
    await asyncio.gather(reconnect_task, return_exceptions=True)
    await tcp_server.stop()
    await serial_conn.disconnect()

    logger.info("grbl-proxy stopped.")


def run() -> None:
    """Console script entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="GRBL Laser Proxy")
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=None,
        help="Path to config.yaml (default: ~/.grbl-proxy/config.yaml)",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_main(config_path=args.config, debug=args.debug))
    except KeyboardInterrupt:
        pass
