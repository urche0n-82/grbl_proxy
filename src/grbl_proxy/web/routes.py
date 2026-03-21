"""REST API routes and WebSocket handler for grbl-proxy."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse

if TYPE_CHECKING:
    from grbl_proxy.web.status import ProxyStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _status(request: Request):
    return request.app.state.proxy_status


def _control(request: Request):
    return request.app.state.proxy_control


def _console(request: Request):
    return request.app.state.console_log


def _config(request: Request):
    return request.app.state.config


def _ws_manager(request: Request) -> "WebSocketManager":
    return request.app.state.ws_manager


# ---------------------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------------------


class WebSocketManager:
    """Manages active WebSocket connections and broadcasts status snapshots."""

    EXECUTING_STATES = {"Executing", "Paused"}

    def __init__(self, status: "ProxyStatus") -> None:
        self._status = status
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        try:
            await ws.receive_text()  # block until client disconnects
        except Exception:
            pass
        finally:
            self._connections.discard(ws)

    async def broadcast_loop(self) -> None:
        """Long-lived background task: rate-adaptive push to all connected clients."""
        while True:
            try:
                snap = self._status.snapshot()
                interval = 0.25 if snap.proxy_state in self.EXECUTING_STATES else 1.0
                if self._connections:
                    payload = json.dumps(dataclasses.asdict(snap))
                    dead: set[WebSocket] = set()
                    for ws in list(self._connections):
                        try:
                            await ws.send_text(payload)
                        except Exception:
                            dead.add(ws)
                    self._connections -= dead
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("broadcast_loop error: %s", exc)
                await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/status")
    async def get_status(request: Request):
        snap = _status(request).snapshot()
        return dataclasses.asdict(snap)

    @router.get("/api/job")
    async def get_job(request: Request):
        snap = _status(request).snapshot()
        return {
            "proxy_state": snap.proxy_state,
            "lines_sent": snap.job_lines_sent,
            "total_lines": snap.job_total_lines,
            "progress_pct": snap.job_progress_pct,
            "elapsed_s": snap.job_elapsed_s,
        }

    @router.post("/api/job")
    async def upload_job(request: Request, file: UploadFile):
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        original_filename = file.filename or None

        content = await file.read()

        def _write():
            storage_dir.mkdir(parents=True, exist_ok=True)
            dest = storage_dir / "uploaded.gcode"
            dest.write_bytes(content)
            # Store original filename alongside so /api/job/start can retrieve it
            if original_filename:
                (storage_dir / "uploaded.filename").write_text(
                    original_filename, encoding="utf-8"
                )
            return content.count(b"\n")

        line_count = await asyncio.to_thread(_write)
        return {"ok": True, "line_count": line_count, "filename": original_filename}

    @router.post("/api/job/start")
    async def start_job(request: Request):
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        # Try to recover original filename from a sidecar file
        sidecar = storage_dir / "uploaded.filename"
        original_filename = None
        if sidecar.exists():
            try:
                original_filename = sidecar.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass
        ok, reason = await _control(request).start_uploaded_job(
            storage_dir, original_filename=original_filename
        )
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except Exception:
                pass
        return {"ok": True}

    @router.post("/api/job/pause")
    async def pause_job(request: Request):
        ok, reason = await _control(request).pause()
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        return {"ok": True}

    @router.post("/api/job/resume")
    async def resume_job(request: Request):
        ok, reason = await _control(request).resume()
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        return {"ok": True}

    @router.post("/api/job/cancel")
    async def cancel_job(request: Request):
        ok, reason = await _control(request).cancel()
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        return {"ok": True}

    @router.get("/api/console")
    async def get_console(request: Request, n: int = 50):
        return _console(request).recent(n)

    @router.post("/api/console")
    async def post_console(request: Request):
        body = await request.json()
        command = body.get("command", "").strip()
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        ok, reason = await _control(request).send_console(command)
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        return {"ok": True}

    @router.get("/api/jobs")
    async def get_jobs(request: Request):
        from grbl_proxy.job_buffer import load_job_history
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        return load_job_history(storage_dir, cfg.job.max_history)

    @router.get("/api/jobs/{filename}/download")
    async def download_job(request: Request, filename: str):
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        # filename should be like "20250321_143022" (no extension)
        gcode_path = storage_dir / f"{filename}.gcode"
        if not gcode_path.exists():
            raise HTTPException(status_code=404, detail="Job file not found")
        return FileResponse(
            path=str(gcode_path),
            media_type="text/plain",
            filename=f"{filename}.gcode",
        )

    @router.get("/api/settings")
    async def get_settings(request: Request):
        cfg = _config(request)
        return dataclasses.asdict(cfg)

    @router.websocket("/ws/status")
    async def ws_status(websocket: WebSocket):
        manager = _ws_manager(websocket)
        await manager.connect(websocket)

    return router
