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
            # A fresh upload becomes the selected job, superseding any prior
            # selection (the server-side record /api/job/start falls back to).
            (storage_dir / "selected.stem").write_text("uploaded", encoding="utf-8")
            return content.count(b"\n")

        line_count = await asyncio.to_thread(_write)
        return {"ok": True, "line_count": line_count, "filename": original_filename}

    @router.post("/api/job/start")
    async def start_job(request: Request):
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()

        # Which job to run is identified by a stem. Prefer the {"stem": ...}
        # request body, but fall back to the server-side selection recorded by
        # /api/files/{stem}/select. The fallback matters: a browser running a
        # cached older app.js sends no body, and without it Run would wrongly
        # take the upload path and fail with "No uploaded file found".
        selected_ptr = storage_dir / "selected.stem"
        stem = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                stem = body.get("stem")
        except Exception:
            pass
        if not stem and selected_ptr.exists():
            try:
                stem = selected_ptr.read_text(encoding="utf-8").strip() or None
            except Exception:
                stem = None

        if stem and stem != "uploaded":
            ok, reason = await _control(request).start_existing_job(
                storage_dir, stem, max_history=cfg.job.max_history
            )
            selected_ptr.unlink(missing_ok=True)  # selection consumed
            if not ok:
                raise HTTPException(status_code=409, detail=reason)
            return {"ok": True}

        # Fresh-upload path: recover the original filename from the sidecar,
        # archive uploaded.gcode, and run it.
        sidecar = storage_dir / "uploaded.filename"
        original_filename = None
        if sidecar.exists():
            try:
                original_filename = sidecar.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass
        ok, reason = await _control(request).start_uploaded_job(
            storage_dir,
            original_filename=original_filename,
            max_history=cfg.job.max_history,
        )
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except Exception:
                pass
        selected_ptr.unlink(missing_ok=True)  # selection consumed
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

    @router.get("/api/files")
    async def list_files(request: Request):
        """List all .gcode files in storage dir, newest-modified first.

        Returns each file as:
          { stem, display_name, size_bytes, line_count, modified }
        'uploaded' is the staged-but-not-yet-run file (stem == "uploaded").
        All others are timestamp-named completed/historical jobs.
        """
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        if not storage_dir.exists():
            return []

        def _scan():
            results = []
            for p in sorted(storage_dir.glob("*.gcode"), key=lambda f: f.stat().st_mtime, reverse=True):
                stem = p.stem
                if stem == "current":
                    continue  # in-progress LightBurn capture, not user-visible
                stat = p.stat()
                # Prefer original_filename from sidecar (uploaded.filename) or meta JSON
                display_name = stem
                sidecar = storage_dir / "uploaded.filename"
                meta_path = storage_dir / f"{stem}.meta.json"
                if stem == "uploaded" and sidecar.exists():
                    try:
                        display_name = sidecar.read_text(encoding="utf-8").strip() or stem
                    except Exception:
                        pass
                elif meta_path.exists():
                    try:
                        import json as _json
                        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                        display_name = meta.get("original_filename") or stem
                    except Exception:
                        pass
                line_count = None
                try:
                    line_count = p.read_bytes().count(b"\n")
                except Exception:
                    pass
                results.append({
                    "stem": stem,
                    "display_name": display_name,
                    "size_bytes": stat.st_size,
                    "line_count": line_count,
                    "modified": stat.st_mtime,
                })
            return results

        return await asyncio.to_thread(_scan)

    @router.delete("/api/files/{stem}")
    async def delete_file(request: Request, stem: str):
        """Delete a stored .gcode file (and its sidecar/meta if present)."""
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        gcode_path = storage_dir / f"{stem}.gcode"
        if not gcode_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        def _delete():
            gcode_path.unlink(missing_ok=True)
            (storage_dir / f"{stem}.meta.json").unlink(missing_ok=True)
            if stem == "uploaded":
                (storage_dir / "uploaded.filename").unlink(missing_ok=True)
            # Drop a dangling selection pointing at the file just removed.
            ptr = storage_dir / "selected.stem"
            try:
                if ptr.exists() and ptr.read_text(encoding="utf-8").strip() == stem:
                    ptr.unlink(missing_ok=True)
            except Exception:
                pass

        await asyncio.to_thread(_delete)
        return {"ok": True}

    @router.post("/api/files/{stem}/select")
    async def select_file(request: Request, stem: str):
        """Load a stored file into the Job window (resolve its display name).

        Deliberately does NOT copy the contents anywhere: /api/job/start receives
        this stem and streams the stored {stem}.gcode in place, so re-running a
        saved job never leaves a duplicate behind. (It used to copy the file to
        uploaded.gcode, which start then re-archived under a new timestamped name
        on every run.) The 'uploaded' staging file keeps its sidecar name.
        """
        cfg = _config(request)
        storage_dir = Path(cfg.job.storage_dir).expanduser()
        src = storage_dir / f"{stem}.gcode"
        if not src.exists():
            raise HTTPException(status_code=404, detail="File not found")

        def _select():
            # Record the selection server-side so /api/job/start can run this
            # stem even if the client (e.g. a cached older app.js) sends no body.
            (storage_dir / "selected.stem").write_text(stem, encoding="utf-8")
            sidecar = storage_dir / "uploaded.filename"
            if stem == "uploaded" and sidecar.exists():
                try:
                    return sidecar.read_text(encoding="utf-8").strip() or stem
                except Exception:
                    return stem
            meta_path = storage_dir / f"{stem}.meta.json"
            if meta_path.exists():
                try:
                    import json as _json
                    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                    return meta.get("original_filename") or stem
                except Exception:
                    pass
            return stem

        display_name = await asyncio.to_thread(_select)
        return {"ok": True, "display_name": display_name}

    @router.get("/api/webcam")
    async def get_webcam_config(request: Request):
        cfg = _config(request)
        return {
            "enabled": cfg.webcam.enabled,
            "stream_url": cfg.webcam.stream_url,
        }

    @router.get("/api/settings")
    async def get_settings(request: Request):
        cfg = _config(request)
        return dataclasses.asdict(cfg)

    @router.websocket("/ws/status")
    async def ws_status(websocket: WebSocket):
        manager = _ws_manager(websocket)
        await manager.connect(websocket)

    return router
