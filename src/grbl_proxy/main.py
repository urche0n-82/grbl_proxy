"""Entry point for grbl-proxy.

Wires together config, serial connection, and TCP server. Handles SIGTERM for
clean systemd shutdown. Invoked via the `grbl-proxy` console script.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import signal
import sys
from pathlib import Path

from grbl_proxy.config import load_config, resolve_serial_port
from grbl_proxy.proxy_core import ProxyCore
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


async def _main(config_path: Path | None = None, debug: bool = False, stop_event: asyncio.Event | None = None) -> None:
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

    proxy_core = ProxyCore(config.job)
    tcp_server = TcpServer(config.tcp.host, config.tcp.port, serial_conn, proxy_core=proxy_core)
    try:
        server = await tcp_server.start()
    except OSError as e:
        logger.error("%s", e)
        await serial_conn.disconnect()
        return

    # Graceful shutdown on SIGTERM (systemd) or SIGINT (Ctrl-C).
    loop = asyncio.get_running_loop()
    if stop_event is None:
        stop_event = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        logger.info("Received %s, shutting down...", sig_name)
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, _request_stop, "SIGTERM")
    loop.add_signal_handler(signal.SIGINT, _request_stop, "SIGINT")

    logger.info("grbl-proxy running. Press Ctrl-C to stop.")

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down...")
    logger.info("Shutdown: signalling serial connection")
    serial_conn.signal_shutdown()
    logger.info("Shutdown: cancelling reconnect task")
    reconnect_task.cancel()
    logger.info("Shutdown: waiting for reconnect task")
    await asyncio.gather(reconnect_task, return_exceptions=True)
    logger.info("Shutdown: stopping TCP server")
    await tcp_server.stop()
    logger.info("Shutdown: emergency stop")
    await proxy_core.emergency_stop(serial_conn)
    logger.info("Shutdown: disconnecting serial")
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

    # Use a custom executor so we can shut it down without waiting for
    # blocking threads (e.g. serial.Serial() open calls) on SIGINT/SIGTERM.
    executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="grbl-proxy")
    loop = asyncio.new_event_loop()
    loop.set_default_executor(executor)
    stop_event = asyncio.Event()

    # Install a synchronous SIGINT handler BEFORE the event loop starts.
    # This fires on the main thread regardless of what threads are running,
    # and sets stop_event so _main's graceful shutdown sequence runs.
    def _sigint_handler(_sig, _frame):
        print("SIGINT received — requesting graceful shutdown", flush=True)
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, _sigint_handler)

    main_task = loop.create_task(
        _main(config_path=args.config, debug=args.debug, stop_event=stop_event)
    )
    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        print("KeyboardInterrupt caught in run() — triggering graceful shutdown", flush=True)
        loop.call_soon_threadsafe(stop_event.set)
        loop.run_until_complete(main_task)
    finally:
        print("run() finally block reached", flush=True)
        # Shut down executor without waiting for blocked threads to finish.
        # This allows the process to exit even if a serial.Serial() call is
        # stuck in a thread pool worker.
        executor.shutdown(wait=False, cancel_futures=True)
        loop.close()
