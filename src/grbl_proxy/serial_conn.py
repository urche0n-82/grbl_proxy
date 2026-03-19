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

import serial
import serial.tools.list_ports

from grbl_proxy.config import SerialConfig

logger = logging.getLogger(__name__)

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
        self._serial: serial.Serial | None = None
        self._write_lock = asyncio.Lock()
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
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    async def read_line(self) -> str:
        """Read one newline-terminated line from the serial port.

        Blocks (in a thread) until a complete line arrives or the 1-second
        readline timeout fires. On timeout returns an empty string. On USB
        disconnect raises SerialDisconnectedError.
        """
        if self._serial is None:
            raise SerialDisconnectedError("Serial port not open")

        s = self._serial  # snapshot — may be set to None by close_immediately()
        try:
            raw = await asyncio.to_thread(s.readline)
        except serial.SerialException as e:
            logger.warning("Serial read error: %s", e)
            self.close_immediately()
            raise SerialDisconnectedError(str(e)) from e
        except OSError as e:
            logger.warning("Serial OS error on read: %s", e)
            self.close_immediately()
            raise SerialDisconnectedError(str(e)) from e

        if not raw:
            # Timeout — no data within READLINE_TIMEOUT seconds, not an error
            return ""

        return raw.decode(errors="replace").rstrip("\r\n")

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
                self._connected.clear()
                raise SerialDisconnectedError(str(e)) from e
            except OSError as e:
                logger.warning("Serial OS error on write: %s", e)
                self._connected.clear()
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
        """
        while not self._shutting_down:
            if not self._connected.is_set():
                # Only attempt open if the device file exists — avoids a long
                # blocking serial.Serial() call on a missing/disconnected port.
                if os.path.exists(self._port):
                    logger.info(
                        "Attempting serial reconnect to %s ...", self._port
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(self._open_port),
                            timeout=self._config.reconnect_interval - 0.5,
                        )
                        self._connected.set()
                        logger.info("Serial reconnected to %s", self._port)
                    except SerialDisconnectedError:
                        pass  # will retry after interval
                    except asyncio.TimeoutError:
                        logger.debug("Serial open timed out, will retry")
                else:
                    logger.info(
                        "Waiting for %s to appear ...", self._port
                    )

            await asyncio.sleep(self._config.reconnect_interval)

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
