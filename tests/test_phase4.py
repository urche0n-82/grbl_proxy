"""Phase 4 test suite: web API and dashboard.

Tests the REST API and WebSocketManager using FastAPI's ASGI transport
(httpx.AsyncClient + ASGITransport) — no real server or hardware needed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from grbl_proxy.config import Config, JobConfig, WebConfig
from grbl_proxy.proxy_core import ProxyState
from grbl_proxy.web.app import create_app
from grbl_proxy.web.console_log import ConsoleLog, _ConsoleLogHandler
from grbl_proxy.web.routes import WebSocketManager
from grbl_proxy.web.status import ProxyControl, ProxyStatus, StatusSnapshot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_core(state: ProxyState = ProxyState.PASSTHROUGH) -> MagicMock:
    core = MagicMock()
    core._state = state
    core._last_status = {"state": "Idle", "mpos": (1.0, 2.0, 0.0), "fs": (500, 0)}
    core._streamer = None
    core._buffer = None
    core._serial_conn = None
    return core


def _make_status(core=None) -> ProxyStatus:
    return ProxyStatus(core or _make_mock_core())


def _make_control(core=None) -> ProxyControl:
    return ProxyControl(core or _make_mock_core())


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.job = JobConfig(storage_dir=str(tmp_path))
    cfg.web = WebConfig(host="127.0.0.1", port=8080)
    return cfg


@pytest.fixture
def mock_core():
    return _make_mock_core()


@pytest.fixture
def proxy_status(mock_core):
    return ProxyStatus(mock_core)


@pytest.fixture
def proxy_control(mock_core):
    return ProxyControl(mock_core)


@pytest.fixture
def console():
    return ConsoleLog()


@pytest.fixture
async def client(proxy_status, proxy_control, console, tmp_path):
    cfg = _make_config(tmp_path)
    app = create_app(proxy_status, proxy_control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# StatusSnapshot tests
# ---------------------------------------------------------------------------


def test_snapshot_passthrough(mock_core):
    mock_core._state = ProxyState.PASSTHROUGH
    snap = ProxyStatus(mock_core).snapshot()
    assert snap.proxy_state == "Passthrough"
    assert snap.grbl_state == "Idle"
    assert snap.mpos_x == 1.0
    assert snap.mpos_y == 2.0
    assert snap.mpos_z == 0.0
    assert snap.feed == 500
    assert snap.spindle == 0
    assert snap.job_lines_sent is None
    assert snap.job_total_lines is None
    assert snap.job_progress_pct is None


def test_snapshot_no_status(mock_core):
    mock_core._last_status = None
    snap = ProxyStatus(mock_core).snapshot()
    assert snap.grbl_state is None
    assert snap.mpos_x is None
    assert snap.feed is None


def test_snapshot_executing_with_streamer(mock_core):
    mock_core._state = ProxyState.EXECUTING
    streamer = MagicMock()
    streamer._lines_sent = 50
    streamer._total_lines = 200
    mock_core._streamer = streamer
    snap = ProxyStatus(mock_core).snapshot()
    assert snap.proxy_state == "Executing"
    assert snap.job_lines_sent == 50
    assert snap.job_total_lines == 200
    assert snap.job_progress_pct == 25.0


def test_snapshot_progress_zero_total(mock_core):
    mock_core._state = ProxyState.EXECUTING
    streamer = MagicMock()
    streamer._lines_sent = 0
    streamer._total_lines = 0
    mock_core._streamer = streamer
    snap = ProxyStatus(mock_core).snapshot()
    assert snap.job_progress_pct is None


def test_snapshot_is_frozen():
    snap = ProxyStatus(_make_mock_core()).snapshot()
    with pytest.raises((TypeError, dataclasses.FrozenInstanceError)):
        snap.proxy_state = "Hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


async def test_get_status_passthrough(client):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["proxy_state"] == "Passthrough"
    assert data["grbl_state"] == "Idle"
    assert data["mpos_x"] == pytest.approx(1.0)


async def test_get_status_executing(proxy_control, console, tmp_path):
    core = _make_mock_core(ProxyState.EXECUTING)
    streamer = MagicMock()
    streamer._lines_sent = 10
    streamer._total_lines = 100
    core._streamer = streamer
    status = ProxyStatus(core)
    cfg = _make_config(tmp_path)
    app = create_app(status, proxy_control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["proxy_state"] == "Executing"
    assert data["job_lines_sent"] == 10
    assert data["job_progress_pct"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# GET /api/job
# ---------------------------------------------------------------------------


async def test_get_job_no_active(client):
    resp = await client.get("/api/job")
    assert resp.status_code == 200
    data = resp.json()
    assert data["lines_sent"] is None
    assert data["total_lines"] is None


# ---------------------------------------------------------------------------
# Job control endpoints
# ---------------------------------------------------------------------------


async def test_pause_wrong_state(client):
    resp = await client.post("/api/job/pause")
    assert resp.status_code == 409
    assert "Passthrough" in resp.json()["detail"]


async def test_pause_executing(proxy_control, console, tmp_path):
    core = _make_mock_core(ProxyState.EXECUTING)
    # mock serial_conn.write to avoid real writes
    serial = AsyncMock()
    core._serial_conn = serial
    core._streamer = MagicMock()

    status = ProxyStatus(core)
    control = ProxyControl(core)
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/job/pause")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert core._state == ProxyState.PAUSED
    core._streamer.pause.assert_called_once()
    serial.write.assert_awaited_once_with(b"!")


async def test_resume_wrong_state(client):
    resp = await client.post("/api/job/resume")
    assert resp.status_code == 409


async def test_resume_paused(proxy_control, console, tmp_path):
    core = _make_mock_core(ProxyState.PAUSED)
    serial = AsyncMock()
    core._serial_conn = serial
    core._streamer = MagicMock()

    status = ProxyStatus(core)
    control = ProxyControl(core)
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/job/resume")
    assert resp.status_code == 200
    assert core._state == ProxyState.EXECUTING
    core._streamer.resume.assert_called_once()
    serial.write.assert_awaited_once_with(b"~")


async def test_cancel_wrong_state(client):
    resp = await client.post("/api/job/cancel")
    assert resp.status_code == 409


async def test_cancel_executing(proxy_control, console, tmp_path):
    core = _make_mock_core(ProxyState.EXECUTING)
    serial = AsyncMock()
    core._serial_conn = serial
    core._streamer = MagicMock()

    status = ProxyStatus(core)
    control = ProxyControl(core)
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/job/cancel")
    assert resp.status_code == 200
    core._streamer.cancel.assert_called_once()
    serial.write.assert_awaited_once_with(b"\x18")


# ---------------------------------------------------------------------------
# Console endpoints
# ---------------------------------------------------------------------------


async def test_get_console_empty(client):
    resp = await client.get("/api/console")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_post_console_passthrough(proxy_control, console, tmp_path):
    core = _make_mock_core(ProxyState.PASSTHROUGH)
    serial = AsyncMock()
    core._serial_conn = serial

    status = ProxyStatus(core)
    control = ProxyControl(core)
    cfg = _make_config(tmp_path)
    app = create_app(status, control, console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/console", json={"command": "$$"})
    assert resp.status_code == 200
    serial.write.assert_awaited_once_with(b"$$\n")


async def test_post_console_wrong_state(client):
    # client fixture uses PASSTHROUGH core but serial_conn is None → fails with serial error
    # To test state rejection, use EXECUTING state
    core = _make_mock_core(ProxyState.EXECUTING)
    console = ConsoleLog()
    cfg = _make_config(Path("/tmp"))
    app = create_app(ProxyStatus(core), ProxyControl(core), console, cfg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/console", json={"command": "$$"})
    assert resp.status_code == 409


async def test_post_console_missing_command(client):
    resp = await client.post("/api/console", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------


async def test_get_settings(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "serial" in data
    assert "tcp" in data
    assert "web" in data
    assert "job" in data


# ---------------------------------------------------------------------------
# ConsoleLog and _ConsoleLogHandler
# ---------------------------------------------------------------------------


def test_console_log_ring_buffer():
    log = ConsoleLog(maxlen=3)
    for i in range(5):
        log.add("rx", f"line{i}")
    recent = log.recent(10)
    assert len(recent) == 3
    assert recent[-1]["text"] == "line4"


def test_console_log_recent_limit():
    log = ConsoleLog(maxlen=100)
    for i in range(20):
        log.add("tx", f"cmd{i}")
    assert len(log.recent(5)) == 5
    assert len(log.recent(50)) == 20


def test_console_log_handler_rx():
    import logging
    log = ConsoleLog()
    handler = _ConsoleLogHandler(log)
    record = logging.LogRecord(
        name="grbl_proxy.tcp_server",
        level=logging.DEBUG,
        pathname="",
        lineno=0,
        msg="Serial→TCP: %r",
        args=(b"ok\n",),
        exc_info=None,
    )
    handler.emit(record)
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["dir"] == "rx"
    assert entries[0]["text"] == "ok"


def test_console_log_handler_tx():
    import logging
    log = ConsoleLog()
    handler = _ConsoleLogHandler(log)
    record = logging.LogRecord(
        name="grbl_proxy.tcp_server",
        level=logging.DEBUG,
        pathname="",
        lineno=0,
        msg="Route [Passthrough]: G0 X10",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["dir"] == "tx"
    assert entries[0]["text"] == "G0 X10"


def test_console_log_handler_ignores_other():
    import logging
    log = ConsoleLog()
    handler = _ConsoleLogHandler(log)
    record = logging.LogRecord(
        name="grbl_proxy.tcp_server",
        level=logging.DEBUG,
        pathname="",
        lineno=0,
        msg="Some unrelated log message",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    assert log.recent() == []


# ---------------------------------------------------------------------------
# WebSocketManager broadcast_loop
# ---------------------------------------------------------------------------


async def test_broadcast_loop_sends_snapshot():
    """broadcast_loop sends a valid JSON snapshot to connected clients."""
    core = _make_mock_core(ProxyState.PASSTHROUGH)
    status = ProxyStatus(core)
    manager = WebSocketManager(status)

    messages = []

    class MockWS:
        async def send_text(self, text):
            messages.append(text)

    manager._connections.add(MockWS())

    task = asyncio.create_task(manager.broadcast_loop())
    await asyncio.sleep(1.1)  # wait for one ~1s idle iteration
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(messages) >= 1
    data = json.loads(messages[0])
    assert data["proxy_state"] == "Passthrough"
    assert "grbl_state" in data
    assert "timestamp" in data


async def test_broadcast_loop_executing_fast():
    """broadcast_loop uses 0.25s interval in Executing state."""
    core = _make_mock_core(ProxyState.EXECUTING)
    status = ProxyStatus(core)
    manager = WebSocketManager(status)

    messages = []

    class MockWS:
        async def send_text(self, text):
            messages.append(text)

    manager._connections.add(MockWS())

    task = asyncio.create_task(manager.broadcast_loop())
    await asyncio.sleep(0.8)  # at 4Hz this should give ~3 messages
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(messages) >= 2
