"""TCP server — LightBurn-facing side of the passthrough relay.

Accepts a single TCP connection at a time. When a new client connects while one
is already active, the old connection is dropped (logged as a warning). All data
from LightBurn is forwarded to serial; all data from serial is forwarded back to
LightBurn.

The two relay directions are independent asyncio Tasks so each can block on its
own await without coupling the other direction's latency.
"""

from __future__ import annotations

import asyncio
import logging

from grbl_proxy import grbl_protocol
from grbl_proxy.serial_conn import SerialDisconnectedError

logger = logging.getLogger(__name__)


class TcpServer:
    """Manages a single LightBurn TCP connection and the bidirectional relay."""

    def __init__(self, host: str, port: int, serial_conn) -> None:
        """
        Args:
            host: Bind address (e.g. "0.0.0.0").
            port: TCP port to listen on.
            serial_conn: Any object implementing the SerialConnection interface
                         (read_line, write, connect, disconnect). Accepts
                         MockSerialConnection for testing.
        """
        self._host = host
        self._port = port
        self._serial = serial_conn
        self._current_writer: asyncio.StreamWriter | None = None
        self._relay_tasks: list[asyncio.Task] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> asyncio.Server:
        """Start listening and return the asyncio.Server object."""
        self._server = await asyncio.start_server(
            self._client_connected,
            host=self._host,
            port=self._port,
        )
        addrs = [s.getsockname() for s in self._server.sockets]
        logger.info("TCP server listening on %s", addrs)
        return self._server

    async def stop(self) -> None:
        """Stop the server and drop any active connection."""
        await self._drop_current_client("server stopping")
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("TCP client connected: %s", peer)

        # Drop any existing connection before taking the new one
        await self._drop_current_client("new client connected")

        self._current_writer = writer

        t1 = asyncio.create_task(
            self._tcp_to_serial(reader), name="tcp-to-serial"
        )
        t2 = asyncio.create_task(
            self._serial_to_tcp(writer), name="serial-to-tcp"
        )
        self._relay_tasks = [t1, t2]

        # Wait for both directions; exceptions are captured, not re-raised
        results = await asyncio.gather(t1, t2, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.debug("Relay task ended with: %r", r)

        logger.info("TCP client disconnected: %s", peer)
        self._current_writer = None
        self._relay_tasks = []

    async def _drop_current_client(self, reason: str) -> None:
        """Cancel relay tasks and close the current writer."""
        if not self._relay_tasks and self._current_writer is None:
            return

        logger.warning("Dropping current TCP client: %s", reason)

        for task in self._relay_tasks:
            if not task.done():
                task.cancel()

        if self._relay_tasks:
            await asyncio.gather(*self._relay_tasks, return_exceptions=True)
        self._relay_tasks = []

        if self._current_writer is not None:
            try:
                self._current_writer.close()
                await self._current_writer.wait_closed()
            except Exception:
                pass
            self._current_writer = None

    async def _tcp_to_serial(self, reader: asyncio.StreamReader) -> None:
        """Forward lines from LightBurn (TCP) to the serial port."""
        while True:
            try:
                data = await reader.read(256)
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.debug("TCP read error: %s", e)
                break

            if not data:
                # EOF — LightBurn closed the connection
                break

            logger.debug("TCP→Serial: %r", data)

            try:
                await self._serial.write(data)
            except SerialDisconnectedError as e:
                logger.warning("Serial unavailable during TCP→Serial relay: %s", e)
                # Keep reading from TCP; the reconnect loop will restore serial
                # Once serial comes back, writes will succeed again.

    async def _serial_to_tcp(self, writer: asyncio.StreamWriter) -> None:
        """Forward lines from GRBL (serial) back to LightBurn (TCP)."""
        while True:
            try:
                line = await self._serial.read_line()
            except SerialDisconnectedError as e:
                logger.warning("Serial disconnected during relay: %s", e)
                # Notify LightBurn that the machine is unavailable
                try:
                    writer.write(b"error:9\n")
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                break

            if not line:
                # Timeout tick from read_line — no data, keep looping
                continue

            # Snoop on status reports: log machine state (Phase 2 will act on this)
            if grbl_protocol.is_status_report(line):
                status = grbl_protocol.parse_status_report(line)
                if status:
                    logger.debug("Machine status: %s", status)

            encoded = (line + "\n").encode()
            logger.debug("Serial→TCP: %r", encoded)

            try:
                writer.write(encoded)
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                logger.debug("TCP write error (client gone): %s", e)
                break
