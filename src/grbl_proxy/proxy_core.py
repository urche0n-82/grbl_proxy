"""Proxy state machine and routing logic for grbl-proxy Phase 2.

Sits between TcpServer and SerialConnection. Maintains the current proxy
state (Disconnected / Passthrough / Buffering) and decides what to do with
each byte/line received from LightBurn.

Phase 3 will add EXECUTING and PAUSED states here.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from grbl_proxy import grbl_protocol
from grbl_proxy.config import AutoDetectConfig, JobConfig
from grbl_proxy.grbl_protocol import StatusReport
from grbl_proxy.job_buffer import JobBuffer, JobMetadata

if TYPE_CHECKING:
    from grbl_proxy.streamer import GrblStreamer, StreamerResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class ProxyState(enum.Enum):
    DISCONNECTED = "Disconnected"
    PASSTHROUGH = "Passthrough"
    BUFFERING = "Buffering"
    EXECUTING = "Executing"  # streaming buffered job to GRBL
    PAUSED = "Paused"        # feed hold during execution
    ERROR = "Error"          # streamer stopped due to GRBL error/alarm or cancel


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _normalize_gcode(line: str) -> str:
    """Normalize a G-code line for comparison: uppercase, collapse whitespace,
    strip leading zeros from G/M word numbers (G04 → G4, M03 → M3)."""
    s = " ".join(line.strip().upper().split())
    # Strip leading zeros in word numbers: G04 → G4, M03 → M3, P0.0 stays P0.0
    return re.sub(r'([A-Z])0+(\d)', r'\1\2', s)


def is_program_end_command(line: str) -> bool:
    """Return True if line is M2 or M30 (program end / end of program)."""
    s = line.strip().upper()
    return s in ("M2", "M30") or s.startswith("M2 ") or s.startswith("M30 ")


# ---------------------------------------------------------------------------
# Heuristic burst detector
# ---------------------------------------------------------------------------


class _HeuristicDetector:
    """Sliding-window motion-command ratio detector.

    Triggers when >= line_burst lines arrive within window_ms milliseconds
    AND the fraction of motion commands is >= motion_ratio.
    """

    def __init__(self, cfg: AutoDetectConfig) -> None:
        self._cfg = cfg
        # Each entry: (monotonic_timestamp, is_motion_command)
        self._window: deque[tuple[float, bool]] = deque()

    def feed(self, line: str, now: float | None = None) -> bool:
        """Feed a decoded line. Returns True if burst threshold is met."""
        if now is None:
            now = time.monotonic()

        cutoff = now - (self._cfg.window_ms / 1000.0)
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

        self._window.append((now, grbl_protocol.is_motion_command(line)))

        if len(self._window) < self._cfg.line_burst:
            return False

        motion_count = sum(1 for _, is_motion in self._window if is_motion)
        ratio = motion_count / len(self._window)
        return ratio >= self._cfg.motion_ratio

    def reset(self) -> None:
        self._window.clear()


# ---------------------------------------------------------------------------
# ProxyCore
# ---------------------------------------------------------------------------


class ProxyCore:
    """State machine that routes LightBurn lines to serial or to the buffer.

    Args:
        job_cfg: Job configuration (markers, storage dir, auto-detect settings).
        idle_timeout_s: Seconds of silence before a buffered job is considered
            complete. Default 2.0; pass a smaller value in tests.
    """

    def __init__(self, job_cfg: JobConfig, idle_timeout_s: float = 2.0) -> None:
        self._cfg = job_cfg
        self._storage_dir = Path(job_cfg.storage_dir).expanduser()
        self._idle_timeout_s = idle_timeout_s
        self._state = ProxyState.DISCONNECTED
        self._buffer: JobBuffer | None = None
        self._last_status: StatusReport | None = None
        self._last_error: StreamerResult | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._detector = (
            _HeuristicDetector(job_cfg.auto_detect)
            if job_cfg.auto_detect.enabled
            else None
        )
        # Phase 3: streamer integration
        self._serial_conn = None  # cached from first process_raw_byte call
        self._streamer: GrblStreamer | None = None
        self._streamer_task: asyncio.Task | None = None
        self._serial_readable = asyncio.Event()
        self._serial_readable.set()  # readable by default; cleared during EXECUTING
        # Handshake events for safe serial read handoff to streamer
        self._serial_read_idle = asyncio.Event()
        self._serial_read_idle.set()  # idle by default (no one is reading)
        self._serial_yield = asyncio.Event()  # one-shot: tells _serial_to_tcp to stop reading

    # ------------------------------------------------------------------
    # State transition entry points (called by TcpServer)
    # ------------------------------------------------------------------

    def on_client_connected(self) -> None:
        """Call when LightBurn establishes a TCP connection."""
        if self._state == ProxyState.BUFFERING and self._buffer is not None:
            # Fire-and-forget discard — we can't await here
            asyncio.ensure_future(self._buffer.discard())
            self._buffer = None
        # During EXECUTING/PAUSED/ERROR: job continues (or stays in error state).
        # Allow reconnect without resetting — LightBurn gets synthetic status.
        if self._state in (ProxyState.EXECUTING, ProxyState.PAUSED, ProxyState.ERROR):
            logger.info(
                "ProxyCore: client reconnected during %s — job continues",
                self._state.value,
            )
            return
        self._cancel_idle_timeout()
        self._state = ProxyState.PASSTHROUGH
        if self._detector:
            self._detector.reset()
        logger.info("ProxyCore: client connected → %s", self._state.value)

    async def on_client_disconnected(self) -> None:
        """Call when the LightBurn TCP connection drops. Idempotent."""
        if self._state == ProxyState.DISCONNECTED:
            return  # already handled (double-call guard)
        if self._state == ProxyState.BUFFERING and self._buffer is not None:
            logger.warning("TCP dropped mid-buffer — discarding incomplete job")
            await self._buffer.discard()
            self._buffer = None
        # EXECUTING/PAUSED: disconnect-safe — job continues without LightBurn.
        # State stays EXECUTING/PAUSED; _on_streamer_done handles the transition.
        if self._state in (ProxyState.EXECUTING, ProxyState.PAUSED):
            logger.info(
                "ProxyCore: client disconnected during %s — job execution continues",
                self._state.value,
            )
            self._cancel_idle_timeout()
            if self._detector:
                self._detector.reset()
            return
        self._cancel_idle_timeout()
        self._state = ProxyState.DISCONNECTED
        if self._detector:
            self._detector.reset()
        logger.info("ProxyCore: client disconnected → %s", self._state.value)

    # ------------------------------------------------------------------
    # Routing methods (called by TcpServer per byte / per line)
    # ------------------------------------------------------------------

    async def process_raw_byte(
        self,
        byte: int,
        writer: asyncio.StreamWriter,
        serial_conn,
    ) -> bool:
        """Intercept a single byte before line assembly.

        Returns True if the byte was consumed and should NOT be added to the
        line buffer. Must be called for every incoming byte.
        """
        # Cache serial_conn reference for use by the streamer
        self._serial_conn = serial_conn

        if not grbl_protocol.is_realtime_command(byte):
            return False

        if byte == ord("?"):
            if self._state in (
                ProxyState.BUFFERING,
                ProxyState.EXECUTING,
                ProxyState.PAUSED,
            ):
                await self._write_synthetic_status(writer)
            elif self._state == ProxyState.ERROR:
                # Report alarm state so LightBurn shows the machine as alarmed
                response = grbl_protocol.make_status_response(state="Alarm")
                writer.write(response.encode())
                await writer.drain()
            else:
                # Passthrough: forward to serial
                try:
                    await serial_conn.write(bytes([byte]))
                except Exception as e:
                    logger.debug("Serial unavailable for realtime '?': %s", e)
            return True

        # Feed hold (!), cycle resume (~), soft reset (Ctrl-X) during buffering
        # discard the incomplete job. Extended real-time bytes (>= 0x80, e.g.
        # jog cancel 0x85, overrides) are just forwarded — they don't affect
        # the buffer state.
        _DISCARD_DURING_BUFFER = frozenset([ord("!"), ord("~"), 0x18])
        if self._state == ProxyState.BUFFERING and byte in _DISCARD_DURING_BUFFER:
            logger.warning(
                "Realtime command 0x%02x received during Buffering — discarding job",
                byte,
            )
            if self._buffer is not None:
                await self._buffer.discard()
                self._buffer = None
            self._cancel_idle_timeout()
            self._state = ProxyState.PASSTHROUGH
            if self._detector:
                self._detector.reset()

        # Feed hold during EXECUTING — pause the streamer
        if self._state == ProxyState.EXECUTING and byte == ord("!"):
            if self._streamer is not None:
                self._streamer.pause()
            self._state = ProxyState.PAUSED
            try:
                await serial_conn.write(bytes([byte]))
            except Exception as e:
                logger.debug("Serial write error for feed hold: %s", e)
            return True

        # Cycle resume during PAUSED — resume the streamer
        if self._state == ProxyState.PAUSED and byte == ord("~"):
            if self._streamer is not None:
                self._streamer.resume()
            self._state = ProxyState.EXECUTING
            try:
                await serial_conn.write(bytes([byte]))
            except Exception as e:
                logger.debug("Serial write error for cycle resume: %s", e)
            return True

        # Soft reset during EXECUTING/PAUSED — cancel job
        if self._state in (ProxyState.EXECUTING, ProxyState.PAUSED) and byte == 0x18:
            if self._streamer is not None:
                self._streamer.cancel()
            try:
                await serial_conn.write(bytes([byte]))
            except Exception as e:
                logger.debug("Serial write error for soft reset: %s", e)
            return True

        # Forward the command to serial regardless (BUFFERING path, PASSTHROUGH, etc.)
        try:
            await serial_conn.write(bytes([byte]))
        except Exception as e:
            logger.debug("Serial unavailable for realtime command: %s", e)
        return True

    async def process_client_line(
        self,
        line: str,
        writer: asyncio.StreamWriter,
        serial_conn,
    ) -> None:
        """Route a complete decoded line from LightBurn.

        In PASSTHROUGH: checks for job start trigger; if not triggered,
        forwards the line to serial.

        In BUFFERING: intercepts the line, writes it to the buffer, and
        spoofs an 'ok' response back to LightBurn. Detects job end.
        """
        if self._state == ProxyState.PASSTHROUGH:
            if self._check_job_start(line):
                await self._enter_buffering(writer)
            else:
                try:
                    await serial_conn.write((line + "\n").encode())
                except Exception as e:
                    logger.debug("Serial write error in passthrough: %s", e)
                    raise

        elif self._state in (ProxyState.EXECUTING, ProxyState.PAUSED):
            # Job is running — reject interactive commands with error:9 (busy)
            logger.debug("Command rejected during %s: %s", self._state.value, line)
            writer.write(b"error:9\n")
            await writer.drain()

        elif self._state == ProxyState.ERROR:
            # Blocked until operator clears the error with $X or $H
            upper = line.strip().upper()
            if upper in ("$X", "$H"):
                logger.info("Error cleared via %s — returning to Passthrough", upper)
                try:
                    await serial_conn.write((line + "\n").encode())
                except Exception as e:
                    logger.debug("Serial write error for error clear: %s", e)
                self._state = ProxyState.PASSTHROUGH
                self._last_error = None
                writer.write(b"ok\n")
                await writer.drain()
            else:
                logger.debug("Command rejected in ERROR state: %s", line)
                writer.write(b"error:9\n")
                await writer.drain()

        elif self._state == ProxyState.BUFFERING:
            # Check for end marker first (don't write the marker line itself
            # but do finalize — the file is self-contained without it)
            if _normalize_gcode(line) == _normalize_gcode(self._cfg.end_marker):
                await self._finalize_job()
                await self._spoof_ok(writer)
                return

            if is_program_end_command(line):
                await self._finalize_job(last_line=line)
                await self._spoof_ok(writer)
                return

            # Normal buffered line
            logger.debug("Buffering: %s", line)
            if self._buffer is not None:
                await self._buffer.write_line(line)
            await self._spoof_ok(writer)
            self._reset_idle_timeout()

    # ------------------------------------------------------------------
    # Status snooping (called by TcpServer._serial_to_tcp)
    # ------------------------------------------------------------------

    def update_last_status(self, status: StatusReport) -> None:
        """Cache the most recent GRBL status report for synthetic responses."""
        self._last_status = status

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> ProxyState:
        return self._state

    @property
    def serial_readable(self) -> asyncio.Event:
        """Event that is set when _serial_to_tcp may read from serial.

        Cleared during EXECUTING — the GrblStreamer owns the serial read path.
        Re-set when the streamer finishes or is cancelled.
        """
        return self._serial_readable

    @property
    def serial_read_idle(self) -> asyncio.Event:
        """Event set when _serial_to_tcp is NOT actively reading serial.

        Used as a handshake: _finalize_job waits on this before starting the
        streamer, ensuring no concurrent serial reads.
        """
        return self._serial_read_idle

    @property
    def serial_yield(self) -> asyncio.Event:
        """One-shot signal telling _serial_to_tcp to abandon its current read.

        Set by _finalize_job before waiting for serial_read_idle. Cleared
        after the handshake completes.
        """
        return self._serial_yield

    # ------------------------------------------------------------------
    # Phase 3: Streamer lifecycle
    # ------------------------------------------------------------------

    def _start_streamer(self, meta: JobMetadata) -> None:
        """Create and launch the GrblStreamer task."""
        from grbl_proxy.streamer import GrblStreamer  # lazy import avoids circular

        if self._serial_conn is None:
            # No serial connection cached — cannot execute. Fall back to passthrough.
            logger.error(
                "Cannot start streamer: no serial connection cached. "
                "Returning to Passthrough."
            )
            self._state = ProxyState.PASSTHROUGH
            self._serial_readable.set()
            return

        logger.info(
            "Job buffered: %d lines, %.1fs elapsed → %s — starting execution",
            meta.line_count,
            meta.duration_s,
            meta.path,
        )
        self._streamer = GrblStreamer(
            gcode_path=meta.path,
            serial_conn=self._serial_conn,
            on_done=self._on_streamer_done,
            on_status=self.update_last_status,
        )
        self._streamer_task = asyncio.create_task(
            self._streamer.run(), name="grbl-streamer"
        )

    def _on_streamer_done(self, result: StreamerResult) -> None:
        """Synchronous callback from GrblStreamer when streaming finishes."""
        self._streamer = None
        self._streamer_task = None
        self._serial_yield.clear()
        self._serial_readable.set()  # restore serial to _serial_to_tcp

        if result.completed:
            logger.info(
                "Streaming complete: %d/%d lines sent",
                result.lines_sent,
                result.total_lines,
            )
            if self._state in (ProxyState.EXECUTING, ProxyState.PAUSED):
                self._state = ProxyState.PASSTHROUGH
        else:
            if result.alarm_code is not None:
                logger.error(
                    "GRBL ALARM:%d — proxy in ERROR state. Send $X or $H to clear.",
                    result.alarm_code,
                )
            elif result.error_code is not None:
                logger.error(
                    "GRBL error:%d at line %d — proxy in ERROR state.",
                    result.error_code,
                    result.error_line,
                )
            else:
                logger.warning(
                    "Job cancelled after %d lines — proxy in ERROR state.",
                    result.lines_sent,
                )
            self._last_error = result
            if self._state in (ProxyState.EXECUTING, ProxyState.PAUSED):
                self._state = ProxyState.ERROR

    async def shutdown(self) -> None:
        """Cancel streamer task cleanly. Called by TcpServer.stop()."""
        if self._streamer_task is not None and not self._streamer_task.done():
            self._streamer_task.cancel()
            await asyncio.gather(self._streamer_task, return_exceptions=True)
        self._streamer = None
        self._streamer_task = None
        self._serial_yield.clear()
        self._serial_readable.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_job_start(self, line: str) -> bool:
        """Return True if this line should trigger a transition to Buffering."""
        return _normalize_gcode(line) == _normalize_gcode(self._cfg.start_marker)

    async def _enter_buffering(self, writer: asyncio.StreamWriter) -> None:
        """Transition to BUFFERING and open the job buffer."""
        logger.info("Job start detected — entering Buffering state")
        buf = JobBuffer(self._storage_dir, start_time=time.time())
        try:
            await buf.open()
        except OSError as e:
            logger.error(
                "Cannot open job buffer at %s: %s — staying in Passthrough",
                self._storage_dir,
                e,
            )
            return
        self._buffer = buf
        self._state = ProxyState.BUFFERING
        if self._detector:
            self._detector.reset()
        await self._spoof_ok(writer)
        self._reset_idle_timeout()

    async def _finalize_job(self, last_line: str | None = None) -> None:
        """Flush, close, and start execution of the completed job buffer."""
        if self._buffer is None:
            return
        self._cancel_idle_timeout()
        if last_line is not None:
            await self._buffer.write_line(last_line)
        # Ensure M2 is always the final line so the buffered file is a
        # self-contained executable G-code program.
        if not is_program_end_command(last_line or ""):
            await self._buffer.write_line("M2")
        meta = await self._buffer.finalize()
        self._buffer = None
        self._state = ProxyState.EXECUTING
        self._serial_readable.clear()  # streamer now owns serial reads
        # Handshake: wait for _serial_to_tcp to finish any in-flight serial read
        # before the streamer starts its own reads. Prevents concurrent reads on
        # the same pyserial port (which corrupts character-counting flow control).
        self._serial_yield.set()  # interrupt in-flight read
        try:
            await asyncio.wait_for(self._serial_read_idle.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning(
                "_serial_to_tcp did not become idle within 2s — starting streamer anyway"
            )
        self._serial_yield.clear()  # reset for next time
        self._start_streamer(meta)

    async def _write_synthetic_status(self, writer: asyncio.StreamWriter) -> None:
        """Respond to a '?' query with a synthesized status based on current state."""
        grbl_state = "Hold" if self._state == ProxyState.PAUSED else "Run"
        if self._last_status is not None:
            mpos = self._last_status.get("mpos", (0.0, 0.0, 0.0))
            fs = self._last_status.get("fs", (0, 0))
            response = grbl_protocol.make_status_response(
                state=grbl_state, mpos=mpos, feed=fs[0], spindle=fs[1]
            )
        else:
            response = grbl_protocol.make_status_response(state=grbl_state)
        writer.write(response.encode())
        await writer.drain()

    @staticmethod
    async def _spoof_ok(writer: asyncio.StreamWriter) -> None:
        """Send a synthetic 'ok' response to LightBurn."""
        writer.write(b"ok\n")
        await writer.drain()

    # ------------------------------------------------------------------
    # Idle timeout
    # ------------------------------------------------------------------

    def _reset_idle_timeout(self) -> None:
        """Arm (or re-arm) the idle timeout for job-end detection."""
        self._cancel_idle_timeout()
        loop = asyncio.get_event_loop()
        self._idle_handle = loop.call_later(
            self._idle_timeout_s, self._on_idle_timeout
        )

    def _cancel_idle_timeout(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _on_idle_timeout(self) -> None:
        """Scheduled callback: no new lines for idle_timeout_s during Buffering."""
        self._idle_handle = None
        if self._state == ProxyState.BUFFERING:
            logger.info("Idle timeout — finalizing buffered job")
            asyncio.ensure_future(self._finalize_job())
