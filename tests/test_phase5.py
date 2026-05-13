"""Phase 5 test suite: job history, idle poll, and upload+run.

Tests:
  Feature C — JobBuffer history rotation, load_job_history()
  Feature A — idle poll task, serial_connected in StatusSnapshot
  Feature B — POST /api/job/start, start_uploaded_job()
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from grbl_proxy.config import Config, JobConfig, WebConfig
from grbl_proxy.job_buffer import JobBuffer, JobMetadata, load_job_history
from grbl_proxy.proxy_core import ProxyCore, ProxyState
from grbl_proxy.web.app import create_app
from grbl_proxy.web.console_log import ConsoleLog
from grbl_proxy.web.status import ProxyControl, ProxyStatus, StatusSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_core(state: ProxyState = ProxyState.PASSTHROUGH) -> MagicMock:
    core = MagicMock()
    core._state = state
    core._last_status = {"state": "Idle", "mpos": (0.0, 0.0, 0.0), "fs": (0, 0)}
    core._streamer = None
    core._buffer = None
    core._serial_conn = None
    core._serial_readable = asyncio.Event()
    core._serial_readable.set()
    core._serial_read_idle = asyncio.Event()
    core._serial_read_idle.set()
    core._serial_yield = asyncio.Event()
    return core


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.job = JobConfig(storage_dir=str(tmp_path), max_history=3)
    cfg.web = WebConfig(host="127.0.0.1", port=8080)
    return cfg


def _make_serial(connected: bool = True) -> MagicMock:
    s = AsyncMock()
    s.is_connected = connected
    return s


# ---------------------------------------------------------------------------
# Feature C — JobBuffer history rotation
# ---------------------------------------------------------------------------


async def test_job_buffer_finalize_creates_timestamped_files(tmp_path):
    buf = JobBuffer(tmp_path, start_time=time.time(), max_history=10)
    await buf.open()
    await buf.write_line("G0 X10")
    await buf.write_line("M2")
    meta = await buf.finalize()

    assert meta.path.exists()
    assert meta.path.suffix == ".gcode"
    assert meta.source == "lightburn"
    assert meta.line_count == 2

    # current.gcode must be gone after rename
    assert not (tmp_path / "current.gcode").exists()

    # .meta.json must exist alongside
    stem = meta.path.stem
    meta_file = tmp_path / f"{stem}.meta.json"
    assert meta_file.exists()
    data = json.loads(meta_file.read_text())
    assert data["line_count"] == 2
    assert data["source"] == "lightburn"


async def test_job_buffer_finalize_upload_source(tmp_path):
    buf = JobBuffer(
        tmp_path,
        start_time=time.time(),
        source="upload",
        original_filename="test.gcode",
        max_history=10,
    )
    await buf.open()
    await buf.write_line("G1 X5")
    meta = await buf.finalize()

    assert meta.source == "upload"
    assert meta.original_filename == "test.gcode"

    stem = meta.path.stem
    data = json.loads((tmp_path / f"{stem}.meta.json").read_text())
    assert data["source"] == "upload"
    assert data["original_filename"] == "test.gcode"


async def test_job_buffer_history_rotation(tmp_path):
    """After max_history jobs, oldest pairs must be deleted."""
    max_history = 2
    for i in range(3):
        buf = JobBuffer(tmp_path, start_time=time.time() + i, max_history=max_history)
        await buf.open()
        await buf.write_line(f"G0 X{i}")
        await buf.finalize()
        # Small sleep so timestamps differ
        await asyncio.sleep(0.01)

    meta_files = sorted(tmp_path.glob("*.meta.json"))
    assert len(meta_files) == max_history, f"Expected {max_history}, got {len(meta_files)}: {meta_files}"


async def test_job_buffer_discard_cleans_up(tmp_path):
    buf = JobBuffer(tmp_path, start_time=time.time())
    await buf.open()
    await buf.write_line("G0 X1")
    await buf.discard()
    assert not (tmp_path / "current.gcode").exists()


def test_load_job_history_empty(tmp_path):
    assert load_job_history(tmp_path) == []


async def test_load_job_history_returns_newest_first(tmp_path):
    for i in range(3):
        buf = JobBuffer(tmp_path, start_time=1000.0 + i * 3600, max_history=10)
        await buf.open()
        await buf.write_line("G0")
        await buf.finalize()
        await asyncio.sleep(0.01)

    history = load_job_history(tmp_path, max_history=10)
    assert len(history) == 3
    # Newest-first: start_time should be descending
    times = [h["start_time"] for h in history]
    assert times == sorted(times, reverse=True)


def test_load_job_history_max_cap(tmp_path):
    # Create 3 meta files manually
    for i in range(3):
        p = tmp_path / f"2025030{i}_000000.meta.json"
        p.write_text(json.dumps({"start_time": float(i), "line_count": 1}))
    history = load_job_history(tmp_path, max_history=2)
    assert len(history) == 2


def test_load_job_history_skips_invalid(tmp_path):
    (tmp_path / "bad.meta.json").write_text("not json {{{")
    (tmp_path / "20250320_000000.meta.json").write_text(json.dumps({"start_time": 1.0}))
    history = load_job_history(tmp_path)
    assert len(history) == 1


# ---------------------------------------------------------------------------
# Feature A — StatusSnapshot.serial_connected
# ---------------------------------------------------------------------------


def test_snapshot_serial_connected_true():
    core = _make_mock_core()
    serial = _make_serial(connected=True)
    snap = ProxyStatus(core, serial_conn=serial).snapshot()
    assert snap.serial_connected is True


def test_snapshot_serial_connected_false():
    core = _make_mock_core()
    serial = _make_serial(connected=False)
    snap = ProxyStatus(core, serial_conn=serial).snapshot()
    assert snap.serial_connected is False


def test_snapshot_no_serial_conn_defaults_false():
    core = _make_mock_core()
    snap = ProxyStatus(core).snapshot()
    assert snap.serial_connected is False


async def test_idle_poll_sends_question_mark(tmp_path):
    """Idle poll task sends '?' to serial when in DISCONNECTED state."""
    job_cfg = JobConfig(storage_dir=str(tmp_path))
    core = ProxyCore(job_cfg, idle_timeout_s=0.1)
    # ProxyCore starts in DISCONNECTED

    serial = _make_serial(connected=True)

    core.start_idle_poll(serial, poll_hz=10.0)  # fast for testing
    await asyncio.sleep(0.25)  # wait for a couple of polls
    core.stop_idle_poll()

    assert serial.write.await_count >= 1
    # All calls should be b"?"
    for call in serial.write.await_args_list:
        assert call.args[0] == b"?"


async def test_idle_poll_skips_when_executing(tmp_path):
    """Idle poll must NOT send '?' during EXECUTING state."""
    job_cfg = JobConfig(storage_dir=str(tmp_path))
    core = ProxyCore(job_cfg, idle_timeout_s=0.1)
    core._state = ProxyState.EXECUTING

    serial = _make_serial(connected=True)
    core.start_idle_poll(serial, poll_hz=10.0)
    await asyncio.sleep(0.25)
    core.stop_idle_poll()

    assert serial.write.await_count == 0


async def test_idle_poll_skips_when_disconnected_serial(tmp_path):
    """Idle poll must NOT send '?' if serial is not connected."""
    job_cfg = JobConfig(storage_dir=str(tmp_path))
    core = ProxyCore(job_cfg, idle_timeout_s=0.1)

    serial = _make_serial(connected=False)
    core.start_idle_poll(serial, poll_hz=10.0)
    await asyncio.sleep(0.25)
    core.stop_idle_poll()

    assert serial.write.await_count == 0


# ---------------------------------------------------------------------------
# Feature B — POST /api/job and POST /api/job/start
# ---------------------------------------------------------------------------


async def test_upload_job_saves_file(tmp_path):
    core = _make_mock_core(ProxyState.PASSTHROUGH)
    serial = _make_serial()
    status = ProxyStatus(core, serial_conn=serial)
    control = ProxyControl(core, serial_conn=serial)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)

    gcode = b"G0 X10\nG1 X20\nM2\n"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/job",
            files={"file": ("my_design.gcode", gcode, "text/plain")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["line_count"] == 3
    assert (tmp_path / "uploaded.gcode").exists()
    # filename sidecar
    assert (tmp_path / "uploaded.filename").read_text() == "my_design.gcode"


async def test_start_job_wrong_state(tmp_path):
    """POST /api/job/start should 409 if proxy is executing."""
    core = _make_mock_core(ProxyState.EXECUTING)
    serial = _make_serial()
    status = ProxyStatus(core, serial_conn=serial)
    control = ProxyControl(core, serial_conn=serial)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    # Plant an uploaded file
    (tmp_path / "uploaded.gcode").write_text("G0 X1\nM2\n")
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/job/start")
    assert resp.status_code == 409


async def test_start_job_no_file(tmp_path):
    """POST /api/job/start should 409 if no uploaded.gcode exists."""
    core = _make_mock_core(ProxyState.PASSTHROUGH)
    serial = _make_serial()
    status = ProxyStatus(core, serial_conn=serial)
    control = ProxyControl(core, serial_conn=serial)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/job/start")
    assert resp.status_code == 409
    assert "No uploaded file" in resp.json()["detail"]


async def test_start_job_success(tmp_path):
    """POST /api/job/start archives file and starts the streamer."""
    job_cfg = JobConfig(storage_dir=str(tmp_path))
    core = ProxyCore(job_cfg, idle_timeout_s=0.1)
    core._state = ProxyState.PASSTHROUGH

    serial = _make_serial()
    # Plant an uploaded file
    uploaded = tmp_path / "uploaded.gcode"
    uploaded.write_text("G0 X10\nG1 X20\nM2\n")

    status = ProxyStatus(core, serial_conn=serial)
    control = ProxyControl(core, serial_conn=serial)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)

    with patch.object(core, "_start_streamer") as mock_start:
        app = create_app(status, control, console, cfg)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/job/start")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert not uploaded.exists()  # renamed away
    mock_start.assert_called_once()
    meta: JobMetadata = mock_start.call_args[0][0]
    assert meta.line_count == 3
    assert meta.source == "upload"


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


async def test_get_jobs_empty(tmp_path):
    core = _make_mock_core()
    status = ProxyStatus(core)
    control = ProxyControl(core)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_jobs_returns_history(tmp_path):
    # Create two completed jobs
    for i in range(2):
        buf = JobBuffer(tmp_path, start_time=time.time() + i, max_history=10)
        await buf.open()
        await buf.write_line("G0")
        await buf.finalize()
        await asyncio.sleep(0.01)

    core = _make_mock_core()
    status = ProxyStatus(core)
    control = ProxyControl(core)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/jobs")
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) == 2


async def test_download_job(tmp_path):
    buf = JobBuffer(tmp_path, start_time=time.time(), max_history=10)
    await buf.open()
    await buf.write_line("G0 X5")
    meta = await buf.finalize()
    stem = meta.path.stem

    core = _make_mock_core()
    status = ProxyStatus(core)
    control = ProxyControl(core)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/jobs/{stem}/download")
    assert resp.status_code == 200
    assert b"G0 X5" in resp.content


async def test_download_job_not_found(tmp_path):
    core = _make_mock_core()
    status = ProxyStatus(core)
    control = ProxyControl(core)
    console = ConsoleLog()
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/jobs/99991231_999999/download")
    assert resp.status_code == 404
