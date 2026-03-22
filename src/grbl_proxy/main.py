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

try:
    import uvicorn
    from grbl_proxy.web.app import create_app
    from grbl_proxy.web.console_log import ConsoleLog, _ConsoleLogHandler
    from grbl_proxy.web.status import ProxyControl, ProxyStatus
    _WEB_AVAILABLE = True
except ImportError:
    _WEB_AVAILABLE = False

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

    # Web server (FastAPI/uvicorn) — optional, skipped if fastapi not installed.
    uv_server = None
    web_task = None
    if _WEB_AVAILABLE:
        proxy_status = ProxyStatus(proxy_core, serial_conn=serial_conn)
        proxy_control = ProxyControl(proxy_core, serial_conn=serial_conn)
        console_log = ConsoleLog()
        logging.getLogger("grbl_proxy.tcp_server").addHandler(
            _ConsoleLogHandler(console_log)
        )
        logging.getLogger("grbl_proxy.web.status").addHandler(
            _ConsoleLogHandler(console_log)
        )
        logging.getLogger("grbl_proxy.proxy_core").addHandler(
            _ConsoleLogHandler(console_log)
        )
        web_app = create_app(proxy_status, proxy_control, console_log, config)
        uv_server = uvicorn.Server(
            uvicorn.Config(
                web_app,
                host=config.web.host,
                port=config.web.port,
                loop="none",
                log_level="warning",
            )
        )
        web_task = asyncio.create_task(uv_server.serve(), name="web-server")
        logger.info("  Web dashboard: http://%s:%d", config.web.host, config.web.port)
    else:
        logger.warning("fastapi/uvicorn not installed — web dashboard disabled")

    # Start idle GRBL status poll (1 Hz) so dashboard shows machine state
    # even when LightBurn is not connected.
    proxy_core.start_idle_poll(serial_conn, poll_hz=config.machine.status_poll_hz)

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
    proxy_core.stop_idle_poll()
    if uv_server is not None and web_task is not None:
        logger.info("Shutdown: stopping web server")
        uv_server.should_exit = True
        await asyncio.gather(web_task, return_exceptions=True)
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

    # stop_event is created here (before the loop runs) and passed into _main
    # so that both the sync signal handler below and _main's async signal
    # handlers share the same event object.
    stop_event = asyncio.Event()

    # Synchronous SIGINT handler — runs on the main thread even when a worker
    # thread holds the GIL (e.g. blocked in pyserial readline). Schedules
    # stop_event.set() onto the event loop so _main's shutdown sequence runs.
    def _sigint_sync(_sig, _frame):
        print("\nSIGINT — shutting down gracefully", flush=True)
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, _sigint_sync)

    try:
        loop.run_until_complete(
            _main(config_path=args.config, debug=args.debug, stop_event=stop_event)
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        loop.close()
