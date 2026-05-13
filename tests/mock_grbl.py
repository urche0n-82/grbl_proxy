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

        line = data.decode(errors="replace").strip()
        if not line:
            return

        # Real-time single-byte commands
        if len(line) == 1 and line.encode()[0] in grbl_protocol.REALTIME_COMMANDS:
            if line == "?":
                self._rx_queue.put_nowait(
                    grbl_protocol.make_status_response().rstrip("\n")
                )
            # ! (feed hold) and ~ (cycle resume) produce no response in passthrough
            return

        # Multi-line data may arrive in one write() call if LightBurn batches
        for subline in line.splitlines():
            subline = subline.strip()
            if not subline:
                continue
            if subline == "?":
                self._rx_queue.put_nowait(
                    grbl_protocol.make_status_response().rstrip("\n")
                )
            else:
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
            ).rstrip("\n")
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
