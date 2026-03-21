"""Facade objects that expose ProxyCore state to the web layer.

ProxyStatus provides a read-only snapshot of the current machine and proxy
state. ProxyControl provides async methods for issuing control commands.
Neither class modifies ProxyCore internals beyond following the same code
paths that process_raw_byte already uses.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from grbl_proxy.proxy_core import ProxyCore, ProxyState


@dataclass(frozen=True)
class StatusSnapshot:
    """Serialisation-ready snapshot of current proxy + machine state."""

    proxy_state: str           # ProxyState.value
    grbl_state: str | None     # "Idle", "Run", "Hold", "Alarm", etc.
    mpos_x: float | None
    mpos_y: float | None
    mpos_z: float | None
    feed: int | None
    spindle: int | None
    job_lines_sent: int | None
    job_total_lines: int | None
    job_progress_pct: float | None
    job_elapsed_s: float | None
    timestamp: float           # time.monotonic() at snapshot time


class ProxyStatus:
    """Read-only view of ProxyCore for the web layer."""

    def __init__(self, core: "ProxyCore") -> None:
        self._core = core

    def snapshot(self) -> StatusSnapshot:
        """Return an atomic snapshot of current state.

        All reads are plain attribute accesses — safe to call from any
        coroutine running in the same event loop as the proxy core.
        """
        core = self._core
        state = core._state

        # GRBL machine state from last cached status report
        last = core._last_status
        grbl_state: str | None = last.get("state") if last else None
        mpos_x = mpos_y = mpos_z = None
        feed: int | None = None
        spindle: int | None = None
        if last:
            mpos = last.get("mpos")
            if mpos:
                mpos_x, mpos_y, mpos_z = mpos
            fs = last.get("fs")
            if fs:
                feed, spindle = fs

        # Job progress from active streamer (if any)
        streamer = core._streamer
        lines_sent: int | None = None
        total_lines: int | None = None
        progress_pct: float | None = None
        if streamer is not None:
            lines_sent = streamer._lines_sent
            total_lines = streamer._total_lines
            if total_lines and total_lines > 0:
                progress_pct = round(lines_sent / total_lines * 100, 1)

        # Elapsed time from job buffer start_time
        elapsed_s: float | None = None
        buf = core._buffer
        if buf is not None:
            elapsed_s = round(time.time() - buf._start_time, 1)

        return StatusSnapshot(
            proxy_state=state.value,
            grbl_state=grbl_state,
            mpos_x=mpos_x,
            mpos_y=mpos_y,
            mpos_z=mpos_z,
            feed=feed,
            spindle=spindle,
            job_lines_sent=lines_sent,
            job_total_lines=total_lines,
            job_progress_pct=progress_pct,
            job_elapsed_s=elapsed_s,
            timestamp=time.monotonic(),
        )


class ProxyControl:
    """Async control interface for the web layer.

    Each method checks state preconditions and follows the exact same
    code paths as process_raw_byte to avoid double-transition bugs.

    serial_conn is injected directly so that pause/resume/cancel work even
    when no LightBurn client is connected (ProxyCore._serial_conn is only
    populated lazily on the first TCP byte received).
    """

    def __init__(self, core: "ProxyCore", serial_conn=None) -> None:
        self._core = core
        self._serial = serial_conn

    def _serial_conn(self):
        """Return the best available serial connection reference."""
        return self._serial or self._core._serial_conn

    async def pause(self) -> tuple[bool, str]:
        """Send feed hold. Valid only in EXECUTING state."""
        from grbl_proxy.proxy_core import ProxyState

        core = self._core
        if core._state != ProxyState.EXECUTING:
            return False, f"Cannot pause in state {core._state.value}"
        if core._streamer is not None:
            core._streamer.pause()
        core._state = ProxyState.PAUSED
        serial = self._serial_conn()
        if serial is not None:
            try:
                await serial.write(b"!")
            except Exception:
                pass
        return True, "ok"

    async def resume(self) -> tuple[bool, str]:
        """Send cycle resume. Valid only in PAUSED state."""
        from grbl_proxy.proxy_core import ProxyState

        core = self._core
        if core._state != ProxyState.PAUSED:
            return False, f"Cannot resume in state {core._state.value}"
        if core._streamer is not None:
            core._streamer.resume()
        core._state = ProxyState.EXECUTING
        serial = self._serial_conn()
        if serial is not None:
            try:
                await serial.write(b"~")
            except Exception:
                pass
        return True, "ok"

    async def cancel(self) -> tuple[bool, str]:
        """Cancel a running or paused job. Mirrors the Ctrl-X / 0x18 path."""
        from grbl_proxy.proxy_core import ProxyState

        core = self._core
        if core._state not in (ProxyState.EXECUTING, ProxyState.PAUSED):
            return False, f"No active job in state {core._state.value}"
        if core._streamer is not None:
            core._streamer.cancel()
        # Write soft-reset — _on_streamer_done handles the state transition.
        serial = self._serial_conn()
        if serial is not None:
            try:
                await serial.write(b"\x18")
            except Exception:
                pass
        return True, "ok"

    async def send_console(self, command: str) -> tuple[bool, str]:
        """Send an arbitrary command to GRBL. Valid only in PASSTHROUGH state."""
        from grbl_proxy.proxy_core import ProxyState

        core = self._core
        if core._state != ProxyState.PASSTHROUGH:
            return False, f"Cannot send console command in state {core._state.value}"
        serial = self._serial_conn()
        if serial is None:
            return False, "No serial connection"
        cmd = command.strip()
        try:
            await serial.write((cmd + "\n").encode())
        except Exception as e:
            return False, str(e)
        logger.debug("Web→Serial: %s", cmd)
        return True, "ok"
