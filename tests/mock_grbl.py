"""MockSerialConnection — an in-process fake GRBL device for testing.

Implements the same interface as SerialConnection so TcpServer can be tested
without hardware. Uses asyncio Queues to simulate serial I/O.
"""

from __future__ import annotations

import asyncio

from grbl_proxy import grbl_protocol


class MockSerialConnection:
    """In-process mock that quacks like SerialConnection.

    By default:
    - Any line sent that isn't a real-time command gets an "ok" response queued.
    - "?" real-time queries get a synthesized status response queued.
    - Responses can be injected directly via inject() / inject_status().
    """

    def __init__(self, auto_respond: bool = True):
        self._rx_queue: asyncio.Queue[str] = asyncio.Queue()
        self.tx_log: list[bytes] = []  # all bytes written to "serial"
        self._connected = True
        self._auto_respond = auto_respond

    # --- SerialConnection interface ---

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def close_immediately(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def read_line(self) -> str:
        """Return the next line from the receive queue, or "" on timeout.

        Uses a 1-second timeout to match the real SerialConnection behaviour
        (READLINE_TIMEOUT). This is critical for the serial-yield handshake:
        _serial_to_tcp must be able to finish its in-flight read within a
        bounded time so the streamer can safely take over.
        """
        try:
            return await asyncio.wait_for(self._rx_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return ""

    async def write(self, data: bytes) -> None:
        """Accept bytes from the TCP relay and optionally auto-respond."""
        self.tx_log.append(data)

        if not self._auto_respond:
            return

        # GRBL's serial ISR plucks real-time bytes out of the stream wherever
        # they appear — they never reach the line buffer and never earn an 'ok'.
        # '?' answers with a status report; '!' and '~' answer with nothing.
        remaining = bytearray()
        for b in data:
            if b in grbl_protocol.REALTIME_COMMANDS:
                if b == ord("?"):
                    self._rx_queue.put_nowait(
                        grbl_protocol.make_status_response().rstrip("\r\n")
                    )
            else:
                remaining.append(b)

        # Whatever is left is line data. GRBL acks every newline-terminated
        # line with 'ok' — INCLUDING an empty one, which protocol_main_loop
        # treats as a no-op sync point:
        #     else if (line[0] == 0) { report_status_message(STATUS_OK); }
        # This is what LightBurn's "?\n" relies on: the ISR takes the '?' and
        # the leftover newline still earns its 'ok'.
        # split("\n")[:-1] yields only complete (terminated) lines, so a
        # partial write with no trailing newline correctly acks nothing yet.
        text = remaining.decode(errors="replace")
        for _ in text.split("\n")[:-1]:
            self._rx_queue.put_nowait("ok")

    # --- Test helpers ---

    def inject(self, response: str) -> None:
        """Inject an arbitrary response line into the receive queue."""
        self._rx_queue.put_nowait(response)

    def inject_status(
        self,
        state: str = "Idle",
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        feed: int = 0,
        spindle: int = 0,
    ) -> None:
        """Inject a synthesized GRBL status report."""
        self._rx_queue.put_nowait(
            grbl_protocol.make_status_response(
                state=state, mpos=(x, y, z), feed=feed, spindle=spindle
            ).rstrip("\r\n")
        )

    def last_sent_lines(self) -> list[str]:
        """Return all lines sent to serial as decoded strings."""
        lines = []
        for raw in self.tx_log:
            for line in raw.decode(errors="replace").splitlines():
                line = line.strip()
                if line:
                    lines.append(line)
        return lines

    def clear_tx_log(self) -> None:
        self.tx_log.clear()
