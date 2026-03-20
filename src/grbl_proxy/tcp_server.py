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

# Avoid a circular import: proxy_core imports grbl_protocol but not tcp_server.
# Import ProxyCore lazily via TYPE_CHECKING so it's available for type hints
# but not loaded at module import time.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from grbl_proxy.proxy_core import ProxyCore


class TcpServer:
    """Manages a single LightBurn TCP connection and the bidirectional relay."""

    def __init__(self, host: str, port: int, serial_conn, proxy_core: "ProxyCore | None" = None) -> None:
        """
        Args:
            host: Bind address (e.g. "0.0.0.0").
            port: TCP port to listen on.
            serial_conn: Any object implementing the SerialConnection interface
                         (read_line, write, connect, disconnect). Accepts
                         MockSerialConnection for testing.
            proxy_core: Optional Phase 2+ state machine. When None (default),
                        the server behaves as a pure Phase 1 passthrough relay.
        """
        self._host = host
        self._port = port
        self._serial = serial_conn
        self._proxy = proxy_core
        self._current_writer: asyncio.StreamWriter | None = None
        self._relay_tasks: list[asyncio.Task] = []
        self._server: asyncio.Server | None = None
        self._line_buf: bytearray = bytearray()  # per-connection line reassembly

    async def start(self) -> asyncio.Server:
        """Start listening and return the asyncio.Server object."""
        try:
            self._server = await asyncio.start_server(
                self._client_connected,
                host=self._host,
                port=self._port,
                reuse_address=True,
            )
        except OSError as e:
            raise OSError(
                f"Cannot bind TCP server on port {self._port}: {e}\n"
                f"Is another instance of grbl-proxy already running?\n"
                f"  sudo systemctl stop grbl-proxy\n"
                f"  sudo ss -tlnp | grep {self._port}"
            ) from e
        addrs = [s.getsockname() for s in self._server.sockets]
        logger.info("TCP server listening on %s", addrs)
        return self._server

    async def stop(self) -> None:
        """Stop the server and drop any active connection."""
        await self._drop_current_client("server stopping")
        if self._proxy is not None:
            await self._proxy.shutdown()
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
        # (_drop_current_client also calls on_client_disconnected if proxy set)
        await self._drop_current_client("new client connected")

        self._current_writer = writer
        self._line_buf.clear()

        if self._proxy is not None:
            self._proxy.on_client_connected()

        # Shared event: set by either relay task to signal the other to stop.
        # This avoids FIRST_COMPLETED (which kills the connection on any transient
        # serial error) while still ensuring _serial_to_tcp exits promptly when
        # LightBurn closes the TCP connection (EOF on _tcp_to_serial).
        stop_relay = asyncio.Event()

        t1 = asyncio.create_task(
            self._tcp_to_serial(reader, stop_relay), name="tcp-to-serial"
        )
        t2 = asyncio.create_task(
            self._serial_to_tcp(writer, stop_relay), name="serial-to-tcp"
        )
        self._relay_tasks = [t1, t2]

        results = await asyncio.gather(t1, t2, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.debug("Relay task ended with: %r", r)

        # Natural EOF path: notify proxy of disconnection (idempotent)
        if self._proxy is not None:
            await self._proxy.on_client_disconnected()

        # Close the writer so LightBurn sees a clean TCP disconnect rather than
        # a half-open connection, allowing it to reconnect immediately.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        logger.info("TCP client disconnected: %s", peer)
        # Only clear if this is still the active writer — a new _client_connected
        # may have already replaced it while our gather was being cancelled.
        if self._current_writer is writer:
            self._current_writer = None
            self._relay_tasks = []
        self._line_buf.clear()

    async def _drop_current_client(self, reason: str) -> None:
        """Cancel relay tasks and close the current writer."""
        if not self._relay_tasks and self._current_writer is None:
            return

        logger.warning("Dropping current TCP client: %s", reason)

        # Notify state machine of disconnect before cancelling tasks
        if self._proxy is not None:
            await self._proxy.on_client_disconnected()

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

    async def _tcp_to_serial(
        self, reader: asyncio.StreamReader, stop_relay: asyncio.Event
    ) -> None:
        """Forward lines from LightBurn (TCP) to the serial port."""
        try:
            while not stop_relay.is_set():
                # Race TCP read against stop_relay so this task wakes promptly
                # when serial disconnects (stop_relay set by _serial_to_tcp),
                # rather than blocking until LightBurn sends more data.
                read_task = asyncio.create_task(reader.read(256))
                stop_task = asyncio.create_task(stop_relay.wait())
                try:
                    await asyncio.wait(
                        [read_task, stop_task], return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError:
                    read_task.cancel()
                    stop_task.cancel()
                    raise
                finally:
                    stop_task.cancel()

                if stop_relay.is_set():
                    read_task.cancel()
                    break

                try:
                    data = read_task.result()
                except (ConnectionResetError, BrokenPipeError) as e:
                    logger.debug("TCP read error: %s", e)
                    break

                if not data:
                    # EOF — LightBurn closed the connection
                    break

                if not self._serial.is_connected:
                    # Serial unavailable — respond to each incoming line with
                    # error:9 so LightBurn stops sending and shows alarm state.
                    writer = self._current_writer
                    if writer is not None:
                        lines = data.count(b'\n')
                        if lines:
                            try:
                                writer.write(b"error:9\n" * lines)
                                await writer.drain()
                            except (BrokenPipeError, ConnectionResetError):
                                break
                    continue

                if self._proxy is None:
                    # Phase 1 passthrough: raw byte forwarding
                    try:
                        await self._serial.write(data)
                    except SerialDisconnectedError as e:
                        logger.warning("Serial unavailable during TCP→Serial relay: %s", e)
                else:
                    # Phase 2+: route through ProxyCore
                    writer = self._current_writer
                    if writer is None:
                        break
                    await self._route_bytes(data, writer)
        finally:
            # Signal _serial_to_tcp to exit (LightBurn disconnected or task cancelled)
            stop_relay.set()

    async def _route_bytes(self, data: bytes, writer: asyncio.StreamWriter) -> None:
        """Process incoming bytes through ProxyCore, assembling lines."""
        for byte in data:
            # Check for real-time commands before adding to line buffer
            consumed = await self._proxy.process_raw_byte(byte, writer, self._serial)
            if consumed:
                continue

            self._line_buf.append(byte)

            if byte == ord("\n"):
                line = self._line_buf.decode(errors="replace").rstrip("\r\n")
                self._line_buf.clear()
                if line:  # skip blank lines
                    logger.debug("Route [%s]: %s", self._proxy.state.value, line)
                    try:
                        await self._proxy.process_client_line(line, writer, self._serial)
                    except SerialDisconnectedError as e:
                        logger.warning("Serial unavailable during routing: %s", e)

    async def _serial_to_tcp(
        self, writer: asyncio.StreamWriter, stop_relay: asyncio.Event
    ) -> None:
        """Forward lines from GRBL (serial) back to LightBurn (TCP)."""
        serial_was_connected = self._serial.is_connected
        while not stop_relay.is_set():
            # During EXECUTING the GrblStreamer owns the serial read path.
            # Wait here until the streamer releases it (serial_readable is set).
            if self._proxy is not None and not self._proxy.serial_readable.is_set():
                # Signal that we are NOT reading serial — safe for streamer to start
                self._proxy.serial_read_idle.set()
                await self._proxy.serial_readable.wait()
                if stop_relay.is_set():
                    break
                continue

            # Signal that we ARE about to read serial
            if self._proxy is not None:
                self._proxy.serial_read_idle.clear()

            # Race read_line() against stop_relay and serial_yield so
            # _serial_to_tcp wakes up promptly when the TCP side closes OR
            # when _finalize_job needs to hand off serial to the streamer.
            read_task = asyncio.create_task(self._serial.read_line())
            stop_task = asyncio.create_task(stop_relay.wait())

            yield_task = None
            if self._proxy is not None:
                yield_task = asyncio.create_task(self._proxy.serial_yield.wait())

            wait_set = {read_task, stop_task}
            if yield_task is not None:
                wait_set.add(yield_task)

            try:
                await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                read_task.cancel()
                stop_task.cancel()
                if yield_task is not None:
                    yield_task.cancel()
                # Only close serial if we own the read path. During
                # EXECUTING/PAUSED the streamer owns it — closing here
                # would kill the actively streaming job.
                if self._proxy is None or self._proxy.serial_readable.is_set():
                    self._serial.close_immediately()
                raise
            finally:
                stop_task.cancel()
                if yield_task is not None:
                    yield_task.cancel()

            # Check if _finalize_job requested us to yield serial reads.
            # Wait for the in-flight read thread to finish (can't cancel a
            # thread) so the serial fd is not accessed concurrently, then
            # signal idle and loop back to the serial_readable check.
            if self._proxy is not None and self._proxy.serial_yield.is_set():
                try:
                    await asyncio.wait_for(asyncio.shield(read_task), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    read_task.cancel()
                self._proxy.serial_read_idle.set()
                continue  # back to top → serial_readable check → waits

            if stop_relay.is_set():
                logger.debug("_serial_to_tcp: stop_relay set, exiting")
                read_task.cancel()
                # Close the port immediately so the readline thread unblocks
                # — but only if we own the read path. During EXECUTING/PAUSED
                # the streamer owns serial; closing it would kill the job.
                if self._proxy is None or self._proxy.serial_readable.is_set():
                    self._serial.close_immediately()
                break

            # Signal idle now that the read has completed
            if self._proxy is not None:
                self._proxy.serial_read_idle.set()

            try:
                line = read_task.result()
            except SerialDisconnectedError as e:
                if serial_was_connected:
                    logger.warning("Serial disconnected during relay: %s", e)
                    # Serial dropped mid-relay — notify LightBurn with an alarm.
                    try:
                        writer.write(b"error:9\n")
                        await writer.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    stop_relay.set()
                    break
                else:
                    # Serial not yet available — pause before retrying so
                    # LightBurn's reconnect attempts don't spin at full speed.
                    await asyncio.sleep(1.0)
                    continue

            if not line:
                # Timeout tick from read_line — no data, keep looping
                serial_was_connected = True  # read succeeded, serial is live
                continue

            serial_was_connected = True  # successful read confirms serial is live

            # Snoop on status reports: log and cache for synthetic responses
            if grbl_protocol.is_status_report(line):
                status = grbl_protocol.parse_status_report(line)
                if status:
                    logger.debug("Machine status: %s", status)
                    if self._proxy is not None:
                        self._proxy.update_last_status(status)

            encoded = (line + "\n").encode()
            logger.debug("Serial→TCP: %r", encoded)

            try:
                writer.write(encoded)
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                logger.debug("TCP write error (client gone): %s", e)
                stop_relay.set()
                break
