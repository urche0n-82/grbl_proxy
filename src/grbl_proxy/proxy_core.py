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
import time
from collections import deque
from pathlib import Path

from grbl_proxy import grbl_protocol
from grbl_proxy.config import AutoDetectConfig, JobConfig
from grbl_proxy.grbl_protocol import StatusReport
from grbl_proxy.job_buffer import JobBuffer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class ProxyState(enum.Enum):
    DISCONNECTED = "Disconnected"
    PASSTHROUGH = "Passthrough"
    BUFFERING = "Buffering"
    # EXECUTING and PAUSED added in Phase 3


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


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
        self._idle_handle: asyncio.TimerHandle | None = None
        self._detector = (
            _HeuristicDetector(job_cfg.auto_detect)
            if job_cfg.auto_detect.enabled
            else None
        )

    # ------------------------------------------------------------------
    # State transition entry points (called by TcpServer)
    # ------------------------------------------------------------------

    def on_client_connected(self) -> None:
        """Call when LightBurn establishes a TCP connection."""
        if self._state == ProxyState.BUFFERING and self._buffer is not None:
            # Fire-and-forget discard — we can't await here
            asyncio.ensure_future(self._buffer.discard())
            self._buffer = None
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
        if not grbl_protocol.is_realtime_command(byte):
            return False

        if byte == ord("?"):
            if self._state == ProxyState.BUFFERING:
                await self._write_synthetic_status(writer)
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
        # Forward the command to serial regardless
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
                await self._enter_buffering(line, writer)
            else:
                try:
                    await serial_conn.write((line + "\n").encode())
                except Exception as e:
                    logger.debug("Serial write error in passthrough: %s", e)
                    raise

        elif self._state == ProxyState.BUFFERING:
            # Check for end marker first (don't write the marker line itself
            # but do finalize — the file is self-contained without it)
            if line.strip() == self._cfg.end_marker:
                await self._finalize_job()
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_job_start(self, line: str) -> bool:
        """Return True if this line should trigger a transition to Buffering."""
        return line.strip() == self._cfg.start_marker

    async def _enter_buffering(self, first_line: str, writer: asyncio.StreamWriter) -> None:
        """Transition to BUFFERING, open the job buffer, write the first line."""
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
        await self._buffer.write_line(first_line)
        await self._spoof_ok(writer)
        self._reset_idle_timeout()

    async def _finalize_job(self, last_line: str | None = None) -> None:
        """Flush, close, and log the completed job buffer."""
        if self._buffer is None:
            return
        self._cancel_idle_timeout()
        if last_line is not None:
            await self._buffer.write_line(last_line)
        meta = await self._buffer.finalize()
        self._buffer = None
        self._state = ProxyState.PASSTHROUGH  # Phase 3: change to EXECUTING
        logger.info(
            "Job buffered: %d lines, %.1fs elapsed → %s  (Phase 3 will execute)",
            meta.line_count,
            meta.duration_s,
            meta.path,
        )

    async def _write_synthetic_status(self, writer: asyncio.StreamWriter) -> None:
        """Respond to a '?' query with a synthesized Run status."""
        if self._last_status is not None:
            mpos = self._last_status.get("mpos", (0.0, 0.0, 0.0))
            fs = self._last_status.get("fs", (0, 0))
            response = grbl_protocol.make_status_response(
                state="Run", mpos=mpos, feed=fs[0], spindle=fs[1]
            )
        else:
            response = grbl_protocol.make_status_response(state="Run")
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
