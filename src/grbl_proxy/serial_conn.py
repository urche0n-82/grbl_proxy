"""Serial port management for grbl-proxy.

Uses pyserial (blocking) wrapped in asyncio.to_thread() so the asyncio event
loop is never blocked. Do NOT use serial_asyncio — it is poorly maintained and
fragile on Raspberry Pi.

Critical: DTR and RTS must be disabled to prevent the ESP32-S2 from resetting
every time the serial port is opened.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import serial
import serial.tools.list_ports

from grbl_proxy.config import SerialConfig, list_serial_candidates

logger = logging.getLogger(__name__)

# How often the reconnect loop wakes to check on the device. Deliberately
# shorter than reconnect_interval: noticing that a node has vanished is urgent
# (we must drop the fd — see run_reconnect_loop), whereas retrying an open is
# not. Open attempts stay throttled to reconnect_interval.
PORT_MONITOR_INTERVAL = 1.0

# How long a device node must exist before we try to open it. A node appears the
# moment the controller enumerates, but this firmware then boots for several
# seconds and re-initialises its USB endpoint partway through. Opening into that
# window yields "device reports readiness to read but returned no data" and, far
# worse, pins the minor number so the board returns as ttyACM1.
PORT_SETTLE_SECONDS = 2.0

READLINE_TIMEOUT = 1.0  # seconds — allows reconnection detection loop to tick


class SerialDisconnectedError(OSError):
    """Raised by read_line() / write() when the serial device is not available."""


class SerialConnection:
    """Async wrapper around a pyserial Serial object.

    All blocking pyserial calls are dispatched to a thread via asyncio.to_thread().
    There is never more than one concurrent read in flight (the serial-to-TCP relay
    loop owns the read path). Writes are protected by an asyncio.Lock.
    """

    def __init__(self, config: SerialConfig, port: str | None = None):
        self._config = config
        self._port = port or config.port
        # With serial.port == "auto" the device path must stay re-resolvable:
        # the controller can come back on a different index after a power cycle,
        # and a path resolved once at startup would strand the proxy forever.
        self._auto_detect = config.port == "auto"
        # When the current device node was first seen present (monotonic), used
        # to let it settle before opening. None = not currently present.
        self._port_first_seen: float | None = None
        self._serial: serial.Serial | None = None
        self._write_lock = asyncio.Lock()
        # Serializes reads: at most one readline() worker thread may touch the
        # fd at a time. Two concurrent readline() calls split bytes across
        # readers and corrupt the ok/status stream. Held for the whole read,
        # and (critically) not released on cancellation until the worker thread
        # has finished — see read_line().
        self._read_lock = asyncio.Lock()
        # Holds bytes received but not yet terminated by a newline. A read that
        # returns mid-line MUST keep the fragment here rather than emitting it
        # as a line — otherwise a split read silently fabricates two garbage
        # "lines" and destroys whatever message it cut in half.
        self._rx_buf = bytearray()
        self._connected = asyncio.Event()
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Public interface (same as MockSerialConnection for duck-typing)
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the serial port. Raises SerialDisconnectedError on failure."""
        await asyncio.to_thread(self._open_port)
        self._connected.set()

    def signal_shutdown(self) -> None:
        """Mark the connection as shutting down (synchronous — safe from signal handlers).

        Must be called before cancelling the reconnect task so that any in-progress
        asyncio.to_thread(_open_port) call is skipped on the next iteration.
        """
        self._shutting_down = True

    async def disconnect(self) -> None:
        """Close the serial port cleanly."""
        self._shutting_down = True
        self.close_immediately()

    def close_immediately(self) -> None:
        """Close the serial port synchronously from any thread or coroutine.

        Yanks the underlying file descriptor so any thread blocked in
        readline() unblocks immediately with an exception. Safe to call
        concurrently — idempotent if already closed.
        """
        s = self._serial
        self._serial = None
        self._connected.clear()
        # Drop any partial line — a fragment from the closed session must never
        # be prepended to data from the next one.
        self._rx_buf.clear()
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    async def read_line(self) -> str:
        """Return one complete newline-terminated line from the serial port.

        Returns "" when no complete line is available within READLINE_TIMEOUT
        (not an error — callers treat it as an idle tick). Raises
        SerialDisconnectedError on USB disconnect.

        Framing is done here rather than with pyserial's readline(): readline()
        returns whatever it has when its timeout fires, so a read that lands
        mid-line yields an unterminated fragment that is indistinguishable from
        a real line. That silently splits one message into two garbage ones and
        destroys any ok it was carrying. Instead we accumulate raw bytes in
        _rx_buf and only ever emit content up to a newline; a partial tail stays
        buffered until the rest of it arrives.

        Reads are serialized by _read_lock so two worker threads can never touch
        the fd at once. If this coroutine is cancelled while its worker thread is
        still reading, it waits for that thread before releasing the lock —
        otherwise the next reader would start a second concurrent read. The port
        is NOT closed on cancellation: a client disconnect must not tear down the
        serial link.
        """
        async with self._read_lock:
            while True:
                # Emit a complete line if the buffer already holds one.
                nl = self._rx_buf.find(b"\n")
                if nl >= 0:
                    line = bytes(self._rx_buf[:nl])
                    del self._rx_buf[: nl + 1]
                    return line.decode(errors="replace").rstrip("\r")

                if self._serial is None:
                    raise SerialDisconnectedError("Serial port not open")

                s = self._serial  # snapshot — may be nulled by close_immediately()
                # Block for at least one byte, then take everything buffered.
                fut = asyncio.ensure_future(
                    asyncio.to_thread(lambda: s.read(max(1, s.in_waiting)))
                )
                try:
                    chunk = await asyncio.shield(fut)
                except asyncio.CancelledError:
                    await asyncio.gather(fut, return_exceptions=True)
                    raise
                except serial.SerialException as e:
                    logger.warning("Serial read error: %s", e)
                    self.close_immediately()
                    raise SerialDisconnectedError(str(e)) from e
                except OSError as e:
                    logger.warning("Serial OS error on read: %s", e)
                    self.close_immediately()
                    raise SerialDisconnectedError(str(e)) from e

                if not chunk:
                    # Timed out with no new bytes. Keep any partial line buffered
                    # for the next call — never emit it as if it were complete.
                    return ""

                self._rx_buf.extend(chunk)

    async def write(self, data: bytes) -> None:
        """Write bytes to the serial port.

        Uses a lock to prevent concurrent writes from the TCP relay task and
        any future polling task. Raises SerialDisconnectedError on failure.
        """
        if self._serial is None:
            raise SerialDisconnectedError("Serial port not open")

        async with self._write_lock:
            try:
                await asyncio.to_thread(self._serial.write, data)
            except serial.SerialException as e:
                logger.warning("Serial write error: %s", e)
                # CLOSE, don't merely mark disconnected. A write failure means
                # the device is gone/broken; holding the fd open keeps its
                # kernel node allocated, so the controller re-enumerates as
                # ttyACM1 instead of reclaiming ttyACM0. (The read path already
                # closes here — this one used to just clear _connected, which
                # was the fd leak behind the renumbering.)
                self.close_immediately()
                raise SerialDisconnectedError(str(e)) from e
            except OSError as e:
                logger.warning("Serial OS error on write: %s", e)
                self.close_immediately()
                raise SerialDisconnectedError(str(e)) from e

    async def run_reconnect_loop(self) -> None:
        """Background task: detect disconnection and reconnect automatically.

        Run this as an asyncio.Task alongside the rest of the proxy. It monitors
        the connection state and retries every config.reconnect_interval seconds
        when the port is unavailable.

        The device-file existence check before open() means serial.Serial() is
        only called when the port file is present, keeping the blocking window
        short and ensuring task cancellation (SIGINT/SIGTERM) is not delayed by
        a hung open() on a disconnected USB device.

        The loop ticks at PORT_MONITOR_INTERVAL but only *opens* every
        config.reconnect_interval. The fast tick exists to drop the fd promptly
        when a device node vanishes; the slow retry avoids hammering open().
        """
        next_attempt = 0.0
        while not self._shutting_down:
            now = time.monotonic()

            # Release any fd we shouldn't still be holding, so its kernel node
            # is freed and the controller can reclaim ttyACM0 instead of
            # re-enumerating as ttyACM1. Two cases, both checked on _serial (NOT
            # _connected — an I/O error may have cleared _connected while leaving
            # the fd open, which is exactly the leak that caused the renumber):
            #   • the device node has physically disappeared, or
            #   • we've been marked disconnected but the fd is still open.
            if self._serial is not None:
                if not os.path.exists(self._port):
                    logger.warning(
                        "Serial device %s disappeared — releasing its kernel node",
                        self._port,
                    )
                    self.close_immediately()
                elif not self._connected.is_set():
                    logger.debug(
                        "Releasing stale serial fd on %s after an I/O failure",
                        self._port,
                    )
                    self.close_immediately()

            if not self._connected.is_set():
                self._rescan_port()

                if not os.path.exists(self._port):
                    self._port_first_seen = None
                    logger.debug("Waiting for %s to appear ...", self._port)
                else:
                    # Let a freshly-appeared node settle before opening it: the
                    # controller boots for several seconds after enumerating and
                    # re-initialises its USB endpoint partway through.
                    if self._port_first_seen is None:
                        self._port_first_seen = now
                        logger.debug(
                            "Serial node %s appeared — settling for %.1fs",
                            self._port, PORT_SETTLE_SECONDS,
                        )
                    elif (
                        now - self._port_first_seen >= PORT_SETTLE_SECONDS
                        and now >= next_attempt
                    ):
                        next_attempt = now + self._config.reconnect_interval
                        logger.info(
                            "Attempting serial reconnect to %s ...", self._port
                        )
                        try:
                            await asyncio.wait_for(
                                asyncio.to_thread(self._open_port),
                                timeout=max(
                                    1.0, self._config.reconnect_interval - 0.5
                                ),
                            )
                            self._connected.set()
                            logger.info("Serial reconnected to %s", self._port)
                        except SerialDisconnectedError:
                            pass  # will retry after the interval
                        except asyncio.TimeoutError:
                            logger.debug("Serial open timed out, will retry")

            await asyncio.sleep(PORT_MONITOR_INTERVAL)

    def _rescan_port(self) -> None:
        """Re-resolve the device path when serial.port is 'auto'.

        Without this the path picked at startup is retried forever, so a
        controller that came back on a different index (ttyACM0 → ttyACM1 after
        a power cycle) is never found again. Candidates are newest-first, which
        also steps over a stale node left behind by the previous connection.
        """
        if not self._auto_detect:
            return
        candidates = list_serial_candidates()
        if not candidates:
            return  # keep the current path and keep waiting for it
        newest = candidates[0]
        if newest != self._port:
            logger.info(
                "Serial device moved: %s → %s (candidates: %s)",
                self._port, newest, candidates,
            )
            self._port = newest
            self._port_first_seen = None  # settle timer applies to the new node

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def port(self) -> str:
        return self._port

    # ------------------------------------------------------------------
    # Internal helpers (run in threads via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _open_port(self) -> None:
        """Blocking: open the serial port. Sets _connected on success."""
        if self._shutting_down:
            raise SerialDisconnectedError("Shutting down")

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

        try:
            s = serial.Serial(
                port=self._port,
                baudrate=self._config.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=READLINE_TIMEOUT,
                dsrdtr=False,   # CRITICAL: prevents ESP32-S2 reset on open
                rtscts=False,   # CRITICAL: prevents ESP32-S2 reset on open
                xonxoff=False,
            )
        except serial.SerialException as e:
            raise SerialDisconnectedError(
                f"Cannot open {self._port}: {e}"
            ) from e

        self._serial = s
        logger.info("Serial port %s opened at %d baud", self._port, self._config.baud)
