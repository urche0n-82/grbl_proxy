"""Character-counting GRBL streamer for grbl-proxy Phase 3.

Reads G-code from a buffered file and streams it to the serial port using
GRBL's character-counting flow control protocol. This approach maximises
throughput while respecting GRBL's 128-byte RX buffer.

Owns the serial read path completely during execution — TcpServer's
_serial_to_tcp task yields control via the serial_readable event while
this task is active.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from grbl_proxy import grbl_protocol
from grbl_proxy.serial_conn import SerialDisconnectedError

logger = logging.getLogger(__name__)

RX_BUFFER_SIZE = 128   # GRBL's receive buffer in bytes
POLL_INTERVAL = 0.25   # seconds — 4 Hz status polling during execution


@dataclass
class StreamerResult:
    """Outcome of a completed (or aborted) streaming run."""

    completed: bool         # True only if all lines were sent and ack'd without error
    cancelled: bool         # True if stopped by cancel() or CancelledError
    error_line: int | None  # 1-based line number that caused error:N, or None
    error_code: int | None  # The N in error:N
    alarm_code: int | None  # The N in ALARM:N, or None
    lines_sent: int         # Lines successfully written to serial before stopping
    total_lines: int        # Total non-blank lines in the file


class GrblStreamer:
    """Stream a buffered G-code file to GRBL using character-counting flow control.

    Lifecycle:
        streamer = GrblStreamer(path, serial, on_done=callback)
        task = asyncio.create_task(streamer.run())
        # ... later if needed:
        streamer.pause()   # suspend line sending (feed hold)
        streamer.resume()  # resume after pause
        streamer.cancel()  # abort and call on_done with cancelled=True
    """

    def __init__(
        self,
        gcode_path: Path,
        serial_conn,
        on_done: Callable[[StreamerResult], None],
        on_status: Callable[[grbl_protocol.StatusReport], None] | None = None,
        poll_interval: float = POLL_INTERVAL,
        rx_buffer_size: int = RX_BUFFER_SIZE,
    ) -> None:
        self._path = gcode_path
        self._serial = serial_conn
        self._on_done = on_done
        self._on_status = on_status
        self._poll_interval = poll_interval
        self._rx_buffer_size = rx_buffer_size
        # Pause/resume control: event is *set* when NOT paused (normal operation)
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._cancelled = False
        self._lines_sent = 0
        self._total_lines = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Entry point for asyncio.create_task(). Drives the full lifecycle."""
        result: StreamerResult | None = None
        try:
            lines = await self._load_lines()
            self._total_lines = len(lines)
            logger.info(
                "Streamer: starting execution of %d lines from %s",
                self._total_lines,
                self._path,
            )
            poll_task = asyncio.create_task(self._poll_loop(), name="streamer-poll")
            result = await self._stream_lines(lines)
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        except asyncio.CancelledError:
            result = StreamerResult(
                completed=False,
                cancelled=True,
                error_line=None,
                error_code=None,
                alarm_code=None,
                lines_sent=self._lines_sent,
                total_lines=self._total_lines,
            )
            raise
        except SerialDisconnectedError as e:
            logger.error("Serial disconnected during streaming: %s", e)
            result = StreamerResult(
                completed=False,
                cancelled=True,
                error_line=None,
                error_code=None,
                alarm_code=None,
                lines_sent=self._lines_sent,
                total_lines=self._total_lines,
            )
        finally:
            if result is not None:
                self._on_done(result)

    def pause(self) -> None:
        """Suspend line sending. Called when LightBurn sends feed hold (!)."""
        logger.debug("Streamer: paused")
        self._resume_event.clear()

    def resume(self) -> None:
        """Resume after pause. Called when LightBurn sends cycle resume (~)."""
        logger.debug("Streamer: resumed")
        self._resume_event.set()

    def cancel(self) -> None:
        """Abort streaming. Sets cancelled flag and unblocks any paused wait."""
        logger.debug("Streamer: cancel requested")
        self._cancelled = True
        self._resume_event.set()  # unblock _resume_event.wait() so loop can exit

    @property
    def lines_sent(self) -> int:
        return self._lines_sent

    @property
    def total_lines(self) -> int:
        return self._total_lines

    @property
    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_lines(self) -> list[str]:
        """Read the G-code file in a thread, returning non-blank lines."""
        def _read_sync() -> list[str]:
            with open(self._path, encoding="utf-8") as f:
                return [
                    line.rstrip("\r\n")
                    for line in f
                    if line.strip()
                ]
        return await asyncio.to_thread(_read_sync)

    async def _stream_lines(self, lines: list[str]) -> StreamerResult:
        """Character-counting streaming core.

        Maintains buffer_used (bytes currently in GRBL's RX buffer) and
        line_lengths (deque of byte counts for each in-flight line). Sends
        the next line only when there is room, draining oks as needed.
        """
        buffer_used = 0
        line_lengths: deque[int] = deque()

        for i, line in enumerate(lines):
            # Respect pause (feed hold from LightBurn or web dashboard)
            await self._resume_event.wait()

            if self._cancelled:
                return StreamerResult(
                    completed=False,
                    cancelled=True,
                    error_line=None,
                    error_code=None,
                    alarm_code=None,
                    lines_sent=self._lines_sent,
                    total_lines=len(lines),
                )

            line_bytes = len(line.encode()) + 1  # +1 for \n

            # Drain responses until there is room in GRBL's RX buffer
            while buffer_used + line_bytes > self._rx_buffer_size:
                response = await self._serial.read_line()
                if not response:
                    continue  # serial timeout tick, keep waiting
                if grbl_protocol.is_ok(response):
                    if line_lengths:
                        buffer_used -= line_lengths.popleft()
                elif grbl_protocol.is_error(response):
                    error_code = grbl_protocol.get_error_code(response)
                    logger.error(
                        "GRBL error:%s on line %d (%r)",
                        error_code, i + 1, line
                    )
                    return StreamerResult(
                        completed=False,
                        cancelled=False,
                        error_line=i + 1,
                        error_code=error_code,
                        alarm_code=None,
                        lines_sent=self._lines_sent,
                        total_lines=len(lines),
                    )
                elif grbl_protocol.is_alarm(response):
                    alarm_code = grbl_protocol.get_alarm_code(response)
                    logger.error("GRBL ALARM:%s during streaming", alarm_code)
                    return StreamerResult(
                        completed=False,
                        cancelled=False,
                        error_line=None,
                        error_code=None,
                        alarm_code=alarm_code,
                        lines_sent=self._lines_sent,
                        total_lines=len(lines),
                    )
                elif grbl_protocol.is_status_report(response):
                    if self._on_status:
                        status = grbl_protocol.parse_status_report(response)
                        if status:
                            self._on_status(status)

            # Send the line
            await self._serial.write((line + "\n").encode())
            buffer_used += line_bytes
            line_lengths.append(line_bytes)
            self._lines_sent += 1

        # All lines sent — drain remaining in-flight oks
        while line_lengths:
            response = await self._serial.read_line()
            if not response:
                continue
            if grbl_protocol.is_ok(response):
                line_lengths.popleft()
            elif grbl_protocol.is_error(response):
                error_code = grbl_protocol.get_error_code(response)
                logger.error(
                    "GRBL error:%s in trailing ack (after line %d)",
                    error_code, self._lines_sent
                )
                if line_lengths:
                    line_lengths.popleft()
                return StreamerResult(
                    completed=False,
                    cancelled=False,
                    error_line=self._lines_sent,
                    error_code=error_code,
                    alarm_code=None,
                    lines_sent=self._lines_sent,
                    total_lines=len(lines),
                )
            elif grbl_protocol.is_alarm(response):
                alarm_code = grbl_protocol.get_alarm_code(response)
                logger.error("GRBL ALARM:%s in trailing ack", alarm_code)
                return StreamerResult(
                    completed=False,
                    cancelled=False,
                    error_line=None,
                    error_code=None,
                    alarm_code=alarm_code,
                    lines_sent=self._lines_sent,
                    total_lines=len(lines),
                )
            elif grbl_protocol.is_status_report(response):
                if self._on_status:
                    status = grbl_protocol.parse_status_report(response)
                    if status:
                        self._on_status(status)

        return StreamerResult(
            completed=True,
            cancelled=False,
            error_line=None,
            error_code=None,
            alarm_code=None,
            lines_sent=self._lines_sent,
            total_lines=len(lines),
        )

    async def _poll_loop(self) -> None:
        """Periodically send '?' to GRBL to get position and machine state.

        The responses (status reports) arrive interleaved with ok responses in
        the _stream_lines loop, which handles them via is_status_report().
        """
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                try:
                    await self._serial.write(b"?")
                except SerialDisconnectedError:
                    logger.debug("Streamer poll: serial not available, stopping poll")
                    break
        except asyncio.CancelledError:
            pass
