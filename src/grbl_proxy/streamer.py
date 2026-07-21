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
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from grbl_proxy import grbl_protocol
from grbl_proxy.serial_conn import SerialDisconnectedError

logger = logging.getLogger(__name__)

RX_BUFFER_SIZE = 128   # GRBL's receive buffer in bytes
POLL_INTERVAL = 0.25   # seconds — 4 Hz status polling during execution
# Consecutive Idle status reports that confirm a job is physically complete
# when the final ok(s) never arrive. At 4 Hz polling this is ~0.75s of
# sustained Idle — long enough to rule out a momentary pre-motion Idle.
IDLE_COMPLETE_POLLS = 3

# Bytes of GRBL RX occupancy still counted as "drained". Anything at or below
# this means the controller has parsed everything we sent it.
RX_DRAINED_MARGIN = 8


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
        # Inline polling state (single serial owner — see _maybe_poll).
        self._last_poll = 0.0
        # Largest RX-free figure GRBL has reported, i.e. its true buffer
        # capacity (observed when idle). Used to tell a genuinely drained
        # controller RX from one that is simply busy.
        self._rx_capacity = 0
        # Consecutive status reports showing a drained RX while we still believe
        # bytes are in flight. Only a sustained streak is treated as evidence
        # that our accounting is stale — see _buffer_looks_phantom.
        self._rx_drained_polls = 0

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
            # No separate poll task: _stream_lines emits '?' inline so exactly
            # one coroutine owns the serial port. A concurrent poller makes the
            # request/response ordering nondeterministic and untestable.
            result = await self._stream_lines(lines)
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
        # Messages already parsed out of a serial line but not yet consumed —
        # one line can carry several when the firmware interleaves writes.
        pending: deque[str] = deque()

        for i, line in enumerate(lines):
            # Respect pause (feed hold from LightBurn or web dashboard)
            await self._resume_event.wait()
            await self._maybe_poll()

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

            # Drain responses until there is room in GRBL's RX buffer.
            #
            # This loop MUST stay interruptible: it can block for a long time
            # (waiting on acks), and a cancel arriving mid-drain has to take
            # effect here — checking _cancelled only between lines means a
            # wedged drain ignores every cancel the operator issues.
            idle_polls = 0
            while buffer_used + line_bytes > self._rx_buffer_size:
                if self._cancelled:
                    logger.warning(
                        "Streamer: cancelled while draining acks (after %d lines)",
                        self._lines_sent,
                    )
                    return StreamerResult(
                        completed=False,
                        cancelled=True,
                        error_line=None,
                        error_code=None,
                        alarm_code=None,
                        lines_sent=self._lines_sent,
                        total_lines=len(lines),
                    )
                await self._maybe_poll()
                response = await self._next_response(pending)
                if not response:
                    continue  # serial timeout tick, keep waiting
                if grbl_protocol.is_ok(response):
                    idle_polls = 0
                    # An ack is proof the accounting is tracking — any
                    # drained-RX streak we were building is not a phantom.
                    self._rx_drained_polls = 0
                    if line_lengths:
                        buffer_used -= line_lengths.popleft()
                    else:
                        # An 'ok' with nothing in flight means our send/ack
                        # accounting has desynced (an extra/duplicate ok, or a
                        # miscount). Surface it — a silent swallow here hides the
                        # very thing that later wedges the trailing drain.
                        logger.warning(
                            "Streamer: received 'ok' with no in-flight line — "
                            "send/ack desync (after %d lines sent)",
                            self._lines_sent,
                        )
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
                    status = grbl_protocol.parse_status_report(response)
                    if status:
                        if self._on_status:
                            self._on_status(status)
                        # A sustained drained RX proves our count is stale.
                        # Reset BOTH halves together: buffer_used and
                        # line_lengths must stay consistent or later acks
                        # subtract bytes already removed and drive the counter
                        # negative, silently disabling the window entirely.
                        if self._buffer_looks_phantom(status, buffer_used):
                            logger.warning(
                                "Streamer: GRBL RX drained for %d polls with %d "
                                "byte(s) still marked in flight after %d lines "
                                "— ack lost, resyncing",
                                self._rx_drained_polls, buffer_used,
                                self._lines_sent,
                            )
                            buffer_used = 0
                            line_lengths.clear()
                            self._rx_drained_polls = 0
                            break  # room now — send the next line
                        # Sustained Idle while we're still waiting for buffer
                        # room is a contradiction: an idle GRBL has an empty
                        # planner and a drained RX, so it owes us nothing and
                        # has room for everything. It means an ack went missing
                        # and our byte accounting is stale — without this the
                        # drain spins forever and wedges the job mid-stream.
                        # Reconcile and keep streaming (the job is NOT done;
                        # there are still lines to send).
                        if status.get("state") == "Idle":
                            idle_polls += 1
                            if idle_polls >= IDLE_COMPLETE_POLLS:
                                logger.warning(
                                    "Streamer: GRBL Idle with %d byte(s) still "
                                    "marked in flight after %d lines — ack "
                                    "accounting lost, reconciling and resuming",
                                    buffer_used,
                                    self._lines_sent,
                                )
                                buffer_used = 0
                                line_lengths.clear()
                                idle_polls = 0
                                break  # room now — send the next line
                        else:
                            idle_polls = 0
                else:
                    # Anything that is not ok / error / ALARM / <status> was
                    # previously discarded silently. If GRBL's response was
                    # mangled (e.g. a realtime status report interleaving with
                    # an 'ok' write, producing "o<Idle|...>k"), the ack it
                    # carried vanishes here and buffer_used drifts up for the
                    # rest of the job. Log it so the leak is visible instead of
                    # only surfacing later as "ack accounting lost".
                    logger.warning(
                        "Streamer: unrecognized serial response %r after %d "
                        "lines — if it carried an 'ok', ack accounting just "
                        "drifted by one line",
                        response,
                        self._lines_sent,
                    )

            # Send the line
            await self._serial.write((line + "\n").encode())
            buffer_used += line_bytes
            line_lengths.append(line_bytes)
            self._lines_sent += 1

        # All lines sent — drain remaining in-flight oks.
        #
        # GRBL should emit exactly one ok per line, but that can't be relied on
        # for completion: some firmware (notably ESP32 GRBL forks like the
        # Falcon 2 Pro) don't send a standard ok for M2/M30, and a single
        # dropped/mis-framed ok has the same effect. Either way line_lengths
        # never empties and this loop would spin forever, wedging the proxy in
        # EXECUTING while the machine is already parked and idle.
        #
        # Guard with an authoritative completion signal: once every line is on
        # the machine, a sustained Idle state means the planner is empty and the
        # job is physically done. This is only reached after all lines are sent,
        # so it can never truncate a job mid-stream. Idle is required to persist
        # across a few polls so a momentary pre-motion Idle can't trip it.
        logger.debug(
            "Streamer: all %d lines sent, draining %d trailing ack(s)",
            self._lines_sent,
            len(line_lengths),
        )
        idle_polls = 0
        while line_lengths:
            # Stay cancellable here too — the trailing drain can block for a
            # long time waiting on final acks.
            if self._cancelled:
                logger.warning(
                    "Streamer: cancelled while draining trailing acks "
                    "(after %d lines)",
                    self._lines_sent,
                )
                return StreamerResult(
                    completed=False,
                    cancelled=True,
                    error_line=None,
                    error_code=None,
                    alarm_code=None,
                    lines_sent=self._lines_sent,
                    total_lines=len(lines),
                )
            await self._maybe_poll()
            response = await self._next_response(pending)
            if not response:
                continue
            if grbl_protocol.is_ok(response):
                line_lengths.popleft()
                idle_polls = 0
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
                status = grbl_protocol.parse_status_report(response)
                if status:
                    if self._on_status:
                        self._on_status(status)
                    if status.get("state") == "Idle":
                        idle_polls += 1
                        if idle_polls >= IDLE_COMPLETE_POLLS:
                            logger.warning(
                                "Streamer: GRBL reached Idle with %d line(s) "
                                "still unacked — treating job as complete. "
                                "GRBL likely did not ok the final command "
                                "(e.g. M2/M30). Sent %d/%d lines.",
                                len(line_lengths),
                                self._lines_sent,
                                len(lines),
                            )
                            break
                    else:
                        idle_polls = 0
            else:
                # See the matching branch in the send loop: a response that
                # matches nothing is a silently-lost ack. Surface it.
                logger.warning(
                    "Streamer: unrecognized serial response %r in trailing "
                    "drain (%d line(s) still unacked)",
                    response,
                    len(line_lengths),
                )

        return StreamerResult(
            completed=True,
            cancelled=False,
            error_line=None,
            error_code=None,
            alarm_code=None,
            lines_sent=self._lines_sent,
            total_lines=len(lines),
        )

    async def _maybe_poll(self) -> None:
        """Emit a '?' status query if the poll interval has elapsed.

        Called from within the streaming loop rather than from a concurrent
        task, so a single coroutine owns every byte written to the port. That
        keeps the request/response sequence deterministic (and reproducible in
        tests) instead of depending on how two tasks happen to interleave.
        """
        now = time.monotonic()
        if now - self._last_poll < self._poll_interval:
            return
        self._last_poll = now
        try:
            await self._serial.write(b"?")
        except SerialDisconnectedError:
            logger.debug("Streamer poll: serial unavailable, skipping")

    async def _next_response(self, pending: deque[str]) -> str:
        """Return the next GRBL protocol message, or "" on an idle tick.

        Reads a line only when the pending queue is empty, then splits it —
        one serial line can carry more than one message when the firmware
        interleaves a status report with an ack (see split_responses). Pulling
        messages through this queue means the drain loops still see exactly one
        message per iteration.
        """
        if not pending:
            line = await self._serial.read_line()
            if not line:
                return ""
            pending.extend(grbl_protocol.split_responses(line))
            if not pending:
                return ""
        return pending.popleft()

    def _buffer_looks_phantom(
        self, status: grbl_protocol.StatusReport, buffer_used: int
    ) -> bool:
        """True when GRBL's own report proves our in-flight count is stale.

        Deliberately conservative. `buffer_used` counts bytes *sent but not yet
        acked*; GRBL's `Bf:` rx_free reflects bytes *still unparsed in its RX
        buffer*. Those are different quantities — the controller frees RX space
        the moment it parses a line, well before that line's ok reaches us — so
        `capacity - rx_free` is legitimately smaller than `buffer_used` during
        completely healthy streaming. Treating that ordinary gap as drift fires
        constantly and, worse, desyncs `buffer_used` from `line_lengths`.

        So only a *drained* RX counts, and only when sustained: acks never take
        IDLE_COMPLETE_POLLS polls (~0.75s at 4 Hz) to arrive, so a controller
        that keeps reporting an empty RX while we still believe bytes are
        outstanding has genuinely lost us an ack.
        """
        bf = status.get("bf")
        if not bf:
            return False
        rx_free = bf[1]
        # Capacity == the most free space ever reported (i.e. when fully idle).
        self._rx_capacity = max(self._rx_capacity, rx_free)

        if not self._rx_capacity or buffer_used <= 0:
            self._rx_drained_polls = 0
            return False

        if (self._rx_capacity - rx_free) > RX_DRAINED_MARGIN:
            self._rx_drained_polls = 0  # RX still holds data — normal, not drift
            return False

        self._rx_drained_polls += 1
        return self._rx_drained_polls >= IDLE_COMPLETE_POLLS
