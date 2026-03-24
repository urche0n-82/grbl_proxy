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
    serial_connected: bool     # True when serial port is open
    timestamp: float           # time.monotonic() at snapshot time


class ProxyStatus:
    """Read-only view of ProxyCore for the web layer."""

    def __init__(self, core: "ProxyCore", serial_conn=None) -> None:
        self._core = core
        self._serial = serial_conn

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

        serial_connected = False
        if self._serial is not None:
            serial_connected = bool(self._serial.is_connected)

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
            serial_connected=serial_connected,
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
        core._user_cancelled = True
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

    async def start_uploaded_job(
        self, storage_dir, original_filename: str | None = None, max_history: int = 20
    ) -> tuple[bool, str]:
        """Start a previously uploaded G-code job. Valid in PASSTHROUGH or DISCONNECTED."""
        from grbl_proxy.proxy_core import ProxyState
        from grbl_proxy.job_buffer import JobMetadata, rotate_history
        import time as _time
        import json as _json
        import re as _re
        from dataclasses import asdict as _asdict
        from pathlib import Path as _Path

        core = self._core
        serial = self._serial_conn()

        if core._state not in (ProxyState.PASSTHROUGH, ProxyState.DISCONNECTED):
            return False, f"Cannot start job in state {core._state.value}"
        if serial is None:
            return False, "No serial connection"

        storage_dir = _Path(storage_dir).expanduser()
        uploaded = storage_dir / "uploaded.gcode"
        if not uploaded.exists():
            return False, "No uploaded file found"

        # Count lines and build metadata
        try:
            line_count = sum(1 for _ in uploaded.open(encoding="utf-8"))
        except Exception as e:
            return False, f"Cannot read uploaded file: {e}"

        start_time = _time.time()

        # Determine the archive stem: use sanitised original filename for uploads,
        # fall back to timestamp (for jobs with no known name).
        def _safe_stem(name: str) -> str:
            stem = _Path(name).stem.strip()
            stem = _re.sub(r"[^\w\-]", "_", stem)
            stem = _re.sub(r"_+", "_", stem).strip("_")
            return stem or "job"

        def _unique_stem(base: str) -> str:
            if not (storage_dir / f"{base}.gcode").exists():
                return base
            n = 2
            while (storage_dir / f"{base}_{n}.gcode").exists():
                n += 1
            return f"{base}_{n}"

        if original_filename:
            stem = _unique_stem(_safe_stem(original_filename))
        else:
            from datetime import datetime as _dt
            stem = _dt.fromtimestamp(start_time).strftime("%Y%m%d_%H%M%S")

        archived_path = storage_dir / f"{stem}.gcode"

        # Rename uploaded.gcode to its permanent name
        try:
            uploaded.rename(archived_path)
        except Exception as e:
            return False, f"Cannot archive uploaded file: {e}"

        meta = JobMetadata(
            path=archived_path,
            line_count=line_count,
            start_time=start_time,
            end_time=start_time,
            duration_s=0.0,
            source="upload",
            original_filename=original_filename,
        )

        # Write meta.json immediately so the Files widget can display the name
        try:
            meta_path = storage_dir / f"{stem}.meta.json"
            data = _asdict(meta)
            data["path"] = str(archived_path)
            meta_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            rotate_history(storage_dir, max_history)
        except Exception as e:
            logger.warning("Could not write meta.json for upload job: %s", e)

        # Wire serial_conn into core so _start_streamer can use it
        if core._serial_conn is None:
            core._serial_conn = serial

        core._state = ProxyState.EXECUTING
        core._serial_readable.clear()
        core._serial_yield.set()
        try:
            import asyncio as _asyncio
            await _asyncio.wait_for(core._serial_read_idle.wait(), timeout=2.0)
        except Exception:
            pass
        core._serial_yield.clear()
        core._start_streamer(meta)

        logger.info("Web-initiated job started: %s (%d lines)", archived_path, line_count)
        return True, "ok"

    async def send_console(self, command: str) -> tuple[bool, str]:
        """Send an arbitrary command to GRBL. Valid in PASSTHROUGH, DISCONNECTED, or ERROR state."""
        from grbl_proxy.proxy_core import ProxyState

        core = self._core
        if core._state not in (ProxyState.PASSTHROUGH, ProxyState.DISCONNECTED, ProxyState.ERROR):
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
        # Clear error state when operator sends $X (unlock) or $H (home)
        if core._state == ProxyState.ERROR and cmd.upper() in ("$X", "$H"):
            logger.info("Error cleared via %s (web console) — returning to Disconnected", cmd)
            core._state = ProxyState.DISCONNECTED
            core._last_error = None
        return True, "ok"
