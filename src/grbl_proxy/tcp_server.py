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
import socket

from grbl_proxy import grbl_protocol
from grbl_proxy.serial_conn import SerialDisconnectedError

logger = logging.getLogger(__name__)

# Max seconds to wait for GRBL's reboot banner after a soft-reset-on-connect.
RESET_BANNER_TIMEOUT = 3.0

# Avoid a circular import: proxy_core imports grbl_protocol but not tcp_server.
# Import ProxyCore lazily via TYPE_CHECKING so it's available for type hints
# but not loaded at module import time.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from grbl_proxy.proxy_core import ProxyCore


class TcpServer:
    """Manages a single LightBurn TCP connection and the bidirectional relay."""

    def __init__(
        self,
        host: str,
        port: int,
        serial_conn,
        proxy_core: "ProxyCore | None" = None,
        reset_on_connect: bool = False,
    ) -> None:
        """
        Args:
            host: Bind address (e.g. "0.0.0.0").
            port: TCP port to listen on.
            serial_conn: Any object implementing the SerialConnection interface
                         (read_line, write, connect, disconnect). Accepts
                         MockSerialConnection for testing.
            proxy_core: Optional Phase 2+ state machine. When None (default),
                        the server behaves as a pure Phase 1 passthrough relay.
            reset_on_connect: When True, soft-reset GRBL (Ctrl-X) on a fresh
                        client connect so each session starts clean. Defaults to
                        False here; main.py wires it from serial.reset_on_connect.
        """
        self._host = host
        self._port = port
        self._serial = serial_conn
        self._proxy = proxy_core
        self._reset_on_connect = reset_on_connect
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

        # Disable Nagle's algorithm: this protocol is small, latency-sensitive
        # line-at-a-time traffic (a lone "ok\n", a single status report). Left
        # at the default, Nagle + delayed ACKs can stall those packets.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as e:
                logger.debug("Could not set TCP_NODELAY: %s", e)

        # Drop any existing connection before taking the new one
        # (_drop_current_client also calls on_client_disconnected if proxy set)
        await self._drop_current_client("new client connected")

        self._current_writer = writer
        self._line_buf.clear()

        if self._proxy is not None:
            # Stop the idle poll and wait for any in-flight poll read to finish
            # BEFORE the relay starts reading, so the two never race on the fd
            # and the poll can't swallow this client's first command response.
            await self._proxy.suspend_idle_poll()
            self._proxy.on_client_connected()

            # Fresh session (Disconnected→Passthrough, not a mid-job reconnect):
            # soft-reset GRBL so it starts clean, matching a direct connection's
            # DTR reset. Done before the relay starts so the reboot doesn't race
            # LightBurn's connect commands (they wait in the TCP buffer). A
            # mid-job reconnect keeps EXECUTING/PAUSED/ERROR and is NOT reset.
            from grbl_proxy.proxy_core import ProxyState
            if self._reset_on_connect and self._proxy.state == ProxyState.PASSTHROUGH:
                await self._reset_grbl_on_connect(writer)

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

    async def _reset_grbl_on_connect(self, writer: asyncio.StreamWriter) -> None:
        """Soft-reset GRBL (Ctrl-X) on a fresh connect for a clean session state.

        A direct serial connection resets the board via DTR on open; the proxy
        keeps DTR disabled, so without this GRBL carries stale in-RAM state into
        the first job (which can hang it). We send the reset and wait for GRBL's
        reboot banner before returning, so LightBurn's connect commands (queued
        in the TCP buffer meanwhile) don't race the reboot. Boot lines are
        forwarded to LightBurn, which expects a GRBL welcome on connect.

        Called while the relay is NOT yet running and the idle poll is suspended,
        so this is the only reader — safe to read serial directly.
        """
        if not self._serial.is_connected:
            logger.debug("reset_on_connect: serial not connected — skipping")
            return
        try:
            await self._serial.write(b"\x18")
        except Exception as e:
            logger.warning("reset_on_connect: could not send soft reset: %s", e)
            return
        logger.info("reset_on_connect: soft reset sent, awaiting GRBL banner")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + RESET_BANNER_TIMEOUT
        while loop.time() < deadline:
            try:
                line = await self._serial.read_line()
            except SerialDisconnectedError:
                break
            if not line:
                continue  # readline timeout tick — keep waiting for the banner
            try:
                writer.write((line + grbl_protocol.LINE_TERMINATOR).encode())
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                break
            if grbl_protocol.is_grbl_greeting(line):
                logger.info("reset_on_connect: GRBL banner received — session clean")
                return
        logger.warning(
            "reset_on_connect: no GRBL banner within %.1fs — proceeding anyway",
            RESET_BANNER_TIMEOUT,
        )

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
                    # Serial not available — drop the TCP connection so LightBurn
                    # shows "disconnected" cleanly rather than looping on error:9.
                    stop_relay.set()
                    break

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
                # Blank lines are NOT skipped. GRBL answers a bare newline with
                # "ok" (an empty line is a no-op sync point), and LightBurn
                # sends "?\n" — the "?" is consumed as a real-time byte above,
                # leaving an empty line whose "ok" LightBurn waits for before
                # sending its next command. Dropping it here stalls the client.
                logger.debug("Route [%s]: %r", self._proxy.state.value, line)
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
                # Do NOT close the serial port here. A client disconnect (or a
                # client swap that cancels this task) must leave the USB link
                # open — closing it forces a reconnect-loop re-open (up to
                # reconnect_interval) that bounces a fast LightBurn reconnect.
                # read_task.cancel() lets read_line() drain its worker thread
                # under _read_lock, so no orphaned readline overlaps the next
                # reader. The port is closed only by disconnect() on shutdown.
                raise
            finally:
                stop_task.cancel()
                if yield_task is not None:
                    yield_task.cancel()

            # Check if _finalize_job requested us to yield serial reads.
            # Wait for the in-flight read thread to finish (can't cancel a
            # thread) so the serial fd is not accessed concurrently, then
            # signal idle and loop back to the serial_readable check.
            #
            # The in-flight read's result is dropped here. That is safe only
            # because GRBL is silent during Buffering (the job is spoofed, not
            # sent), so this read is a timeout. If it ever returns a real line,
            # that line is lost — log it loudly so a genuine drop is visible
            # rather than silently corrupting the streamer's ok accounting.
            if self._proxy is not None and self._proxy.serial_yield.is_set():
                try:
                    dropped = await asyncio.wait_for(
                        asyncio.shield(read_task), timeout=2.0
                    )
                    if dropped:
                        logger.warning(
                            "Serial handoff dropped an in-flight line at the "
                            "Executing transition: %r — GRBL was expected to be "
                            "silent during Buffering.",
                            dropped,
                        )
                except (asyncio.TimeoutError, Exception):
                    read_task.cancel()
                self._proxy.serial_read_idle.set()
                continue  # back to top → serial_readable check → waits

            if stop_relay.is_set():
                logger.debug("_serial_to_tcp: stop_relay set, exiting")
                read_task.cancel()
                # Do NOT close the port on a normal client disconnect — keep the
                # serial link up so an immediate LightBurn reconnect isn't
                # bounced by the reconnect loop. read_task.cancel() drains the
                # in-flight readline thread under _read_lock (no overlap with the
                # next reader). The port is closed only by disconnect() (shutdown)
                # or by read_line() itself on a real serial error.
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
                        writer.write(b"error:9" + grbl_protocol.LINE_TERMINATOR.encode())
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
                # Timeout tick from read_line — no data within READLINE_TIMEOUT.
                # Log it so a freeze can be diagnosed: a steady stream of these
                # during a lockup proves the read loop is alive and GRBL has gone
                # silent (nothing to forward); their ABSENCE proves the read loop
                # itself is stuck (e.g. blocked in writer.drain() on TCP
                # backpressure) — a proxy fault, not a silent GRBL.
                logger.debug("Serial read idle tick (no data, state=%s)",
                             self._proxy.state.value if self._proxy else "n/a")
                serial_was_connected = True  # read succeeded, serial is live
                continue

            serial_was_connected = True  # successful read confirms serial is live

            # One serial line can carry more than one protocol message when the
            # firmware interleaves a realtime status report with an ok/error
            # write. Split them so LightBurn receives well-formed lines rather
            # than a truncated status with an ack glued onto it.
            for message in grbl_protocol.split_responses(line):
                # Snoop on status reports: log and cache for synthetic responses.
                # The report itself is forwarded verbatim — the proxy does not
                # rewrite GRBL's status fields.
                if grbl_protocol.is_status_report(message):
                    status = grbl_protocol.parse_status_report(message)
                    if status:
                        logger.debug("Machine status: %s", status)
                        if self._proxy is not None:
                            self._proxy.update_last_status(status)

                # Re-terminate with CR+LF to match a real GRBL device — read_line()
                # stripped whatever the firmware used. A bare "\n" here can leave a
                # strict LightBurn parser waiting for a line it considers complete.
                encoded = (message + grbl_protocol.LINE_TERMINATOR).encode()
                logger.debug("Serial→TCP: %r", encoded)

                try:
                    writer.write(encoded)
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError) as e:
                    logger.debug("TCP write error (client gone): %s", e)
                    stop_relay.set()
                    break
            if stop_relay.is_set():
                break
