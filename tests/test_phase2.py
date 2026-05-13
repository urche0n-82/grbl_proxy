"""Phase 2 test suite.

Tests cover:
- JobBuffer: file I/O, metadata, discard, finalize
- _HeuristicDetector: burst/ratio logic
- ProxyCore: unit state transitions
- Buffering integration via TcpServer + MockSerialConnection (no hardware)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from grbl_proxy.config import AutoDetectConfig, JobConfig
from grbl_proxy.job_buffer import JobBuffer
from grbl_proxy.proxy_core import ProxyCore, ProxyState, _HeuristicDetector, is_program_end_command
from grbl_proxy.tcp_server import TcpServer
from tests.mock_grbl import MockSerialConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PORT = 19950


def _make_job_cfg(jobs_dir: Path) -> JobConfig:
    return JobConfig(
        storage_dir=str(jobs_dir),
        start_marker="G4 P0.0",
        end_marker="G4 P0.0",
        auto_detect=AutoDetectConfig(
            enabled=True, line_burst=5, window_ms=300, motion_ratio=0.8
        ),
    )


def _make_server(
    port: int,
    jobs_dir: Path,
    auto_respond: bool = True,
    idle_timeout_s: float = 0.1,
) -> tuple[TcpServer, MockSerialConnection, ProxyCore]:
    mock = MockSerialConnection(auto_respond=auto_respond)
    cfg = _make_job_cfg(jobs_dir)
    core = ProxyCore(cfg, idle_timeout_s=idle_timeout_s)
    server = TcpServer("127.0.0.1", port, mock, proxy_core=core)
    return server, mock, core


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def _enter_buffering(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Send the start marker and consume the spoofed ok."""
    writer.write(b"G4 P0.0\n")
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=2.0)


# ---------------------------------------------------------------------------
# TestJobBuffer
# ---------------------------------------------------------------------------


class TestJobBuffer:
    async def test_open_creates_file(self, tmp_path):
        buf = JobBuffer(tmp_path / "jobs")
        await buf.open()
        assert buf.is_open
        assert buf.path is not None
        assert buf.path.exists()

    async def test_write_line_increments_count(self, tmp_path):
        buf = JobBuffer(tmp_path / "jobs")
        await buf.open()
        await buf.write_line("G0 X10")
        await buf.write_line("G1 Y20")
        await buf.write_line("G1 Y30")
        assert buf.line_count == 3

    async def test_discard_removes_file(self, tmp_path):
        buf = JobBuffer(tmp_path / "jobs")
        await buf.open()
        path = buf.path
        await buf.write_line("G0 X10")
        await buf.discard()
        assert not path.exists()

    async def test_finalize_returns_metadata(self, tmp_path):
        buf = JobBuffer(tmp_path / "jobs")
        await buf.open()
        for i in range(5):
            await buf.write_line(f"G0 X{i}")
        meta = await buf.finalize()
        assert meta.line_count == 5
        assert meta.duration_s >= 0
        assert meta.path == buf.path or meta.path is not None

    async def test_finalize_writes_meta_json(self, tmp_path):
        jobs_dir = tmp_path / "jobs"
        buf = JobBuffer(jobs_dir)
        await buf.open()
        await buf.write_line("G0 X0")
        meta = await buf.finalize()
        # Phase 5: finalize renames to timestamped files, not current.*
        stem = meta.path.stem
        meta_path = jobs_dir / f"{stem}.meta.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["line_count"] == 1
        assert "start_time" in data

    async def test_finalized_file_has_correct_content(self, tmp_path):
        buf = JobBuffer(tmp_path / "jobs")
        await buf.open()
        await buf.write_line("G0 X10")
        await buf.write_line("G1 Y20")
        meta = await buf.finalize()
        content = meta.path.read_text()
        assert "G0 X10\n" in content
        assert "G1 Y20\n" in content


# ---------------------------------------------------------------------------
# TestHeuristicDetector
# ---------------------------------------------------------------------------


class TestHeuristicDetector:
    def _cfg(self, burst=5, window_ms=300, ratio=0.8) -> AutoDetectConfig:
        return AutoDetectConfig(
            enabled=True, line_burst=burst, window_ms=window_ms, motion_ratio=ratio
        )

    def test_no_trigger_below_burst(self):
        det = _HeuristicDetector(self._cfg(burst=5))
        for _ in range(4):
            assert det.feed("G0 X10") is False

    def test_triggers_on_burst_with_high_motion_ratio(self):
        det = _HeuristicDetector(self._cfg(burst=5))
        now = time.monotonic()
        # Feed 5 motion commands within 10ms (well within 300ms window)
        for i in range(5):
            result = det.feed("G0 X10", now=now + i * 0.001)
        assert result is True

    def test_no_trigger_when_motion_ratio_low(self):
        det = _HeuristicDetector(self._cfg(burst=5, ratio=0.8))
        now = time.monotonic()
        # 2 motion + 3 queries = 40% motion, below 80%
        lines = ["G0 X10", "?", "?", "G1 Y10", "?"]
        result = False
        for i, line in enumerate(lines):
            result = det.feed(line, now=now + i * 0.001)
        assert result is False

    def test_reset_clears_window(self):
        det = _HeuristicDetector(self._cfg(burst=5))
        now = time.monotonic()
        for i in range(4):
            det.feed("G0 X10", now=now + i * 0.001)
        det.reset()
        # After reset, needs burst again from scratch
        for i in range(4):
            result = det.feed("G0 X10", now=now + 10 + i * 0.001)
        assert result is False

    def test_window_expires_old_entries(self):
        det = _HeuristicDetector(self._cfg(burst=5, window_ms=100))
        t0 = time.monotonic()
        # Feed 3 lines at t0
        for i in range(3):
            det.feed("G0 X10", now=t0 + i * 0.01)
        # Feed 2 more lines 200ms later — old entries should be pruned
        for i in range(2):
            result = det.feed("G0 X10", now=t0 + 0.2 + i * 0.01)
        # Only 2 entries in window — below burst=5
        assert result is False


# ---------------------------------------------------------------------------
# TestIsProgramEndCommand
# ---------------------------------------------------------------------------


class TestIsProgramEndCommand:
    def test_m2(self):
        assert is_program_end_command("M2")
        assert is_program_end_command("m2")
        assert is_program_end_command("M2 ; end")

    def test_m30(self):
        assert is_program_end_command("M30")
        assert is_program_end_command("m30")
        assert is_program_end_command("M30 ; end")

    def test_other_commands(self):
        assert not is_program_end_command("M3")
        assert not is_program_end_command("G0 X10")
        assert not is_program_end_command("ok")


# ---------------------------------------------------------------------------
# TestProxyCoreUnit
# ---------------------------------------------------------------------------


class TestProxyCoreUnit:
    def _core(self, tmp_path) -> ProxyCore:
        return ProxyCore(_make_job_cfg(tmp_path / "jobs"), idle_timeout_s=0.1)

    def test_initial_state_disconnected(self, tmp_path):
        core = self._core(tmp_path)
        assert core.state == ProxyState.DISCONNECTED

    def test_connected_transitions_to_passthrough(self, tmp_path):
        core = self._core(tmp_path)
        core.on_client_connected()
        assert core.state == ProxyState.PASSTHROUGH

    async def test_disconnected_transitions_back(self, tmp_path):
        core = self._core(tmp_path)
        core.on_client_connected()
        await core.on_client_disconnected()
        assert core.state == ProxyState.DISCONNECTED

    async def test_on_client_disconnected_idempotent(self, tmp_path):
        core = self._core(tmp_path)
        # Calling disconnected without connecting should not raise
        await core.on_client_disconnected()
        assert core.state == ProxyState.DISCONNECTED

    def test_update_last_status(self, tmp_path):
        core = self._core(tmp_path)
        status = {"state": "Idle", "mpos": (1.0, 2.0, 0.0), "fs": (3000, 100)}
        core.update_last_status(status)
        assert core._last_status == status


# ---------------------------------------------------------------------------
# TestBufferingMode — integration tests with TcpServer + ProxyCore
# ---------------------------------------------------------------------------


class TestBufferingMode:
    async def test_passthrough_before_job_start(self, tmp_path):
        """A line sent before the start marker is forwarded to serial."""
        server, mock, core = _make_server(_BASE_PORT + 1, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 1)
            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)  # ok from mock
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert "G0 X10" in mock.last_sent_lines()

    async def test_start_marker_triggers_buffering(self, tmp_path):
        """Sending the start marker transitions core to BUFFERING."""
        server, mock, core = _make_server(_BASE_PORT + 2, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 2)
            await _enter_buffering(reader, writer)
            # Small delay so the state transition completes
            await asyncio.sleep(0.05)
            assert core.state == ProxyState.BUFFERING
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_buffered_lines_not_forwarded_to_serial(self, tmp_path):
        """Lines sent during buffering are NOT forwarded to serial."""
        server, mock, core = _make_server(_BASE_PORT + 3, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 3)
            await _enter_buffering(reader, writer)
            mock.clear_tx_log()

            writer.write(b"G0 X50\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)  # spoofed ok

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert "G0 X50" not in mock.last_sent_lines()

    async def test_buffered_lines_get_ok_response(self, tmp_path):
        """Each buffered line gets an 'ok' response back to LightBurn."""
        server, mock, core = _make_server(_BASE_PORT + 4, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 4)
            await _enter_buffering(reader, writer)

            writer.write(b"G0 X10\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=2.0)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert response == b"ok\n"

    async def test_multiple_buffered_lines_each_get_ok(self, tmp_path):
        """Multiple buffered lines each get an independent ok."""
        server, mock, core = _make_server(_BASE_PORT + 5, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 5)
            await _enter_buffering(reader, writer)

            lines = [b"G0 X10\n", b"G1 Y20\n", b"G1 Y30\n"]
            for line in lines:
                writer.write(line)
            await writer.drain()

            responses = []
            for _ in lines:
                r = await asyncio.wait_for(reader.readline(), timeout=2.0)
                responses.append(r)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert all(r == b"ok\n" for r in responses)

    async def test_status_query_during_buffering_returns_run(self, tmp_path):
        """'?' during buffering gets a synthetic <Run|...> response."""
        server, mock, core = _make_server(_BASE_PORT + 6, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 6)
            await _enter_buffering(reader, writer)

            writer.write(b"?")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=2.0)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert response.startswith(b"<Run|")

    async def test_status_query_not_forwarded_to_serial(self, tmp_path):
        """'?' during buffering is NOT forwarded to serial."""
        server, mock, core = _make_server(_BASE_PORT + 7, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 7)
            await _enter_buffering(reader, writer)
            mock.clear_tx_log()

            writer.write(b"?")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        # '?' should not appear in what was sent to the serial mock
        sent = mock.last_sent_lines()
        assert "?" not in sent

    async def test_end_marker_exits_buffering(self, tmp_path):
        """Sending the end marker finalizes the job (exits Buffering → Executing or Passthrough)."""
        server, mock, core = _make_server(_BASE_PORT + 8, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 8)
            await _enter_buffering(reader, writer)

            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            writer.write(b"G4 P0.0\n")
            await writer.drain()
            # Brief wait for finalization; with auto-respond mock the streamer may
            # finish immediately so state can be EXECUTING or PASSTHROUGH.
            await asyncio.sleep(0.1)

            assert core.state in (ProxyState.EXECUTING, ProxyState.PASSTHROUGH)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_m2_finalizes_job(self, tmp_path):
        """M2 command exits Buffering and starts execution (EXECUTING or Passthrough)."""
        server, mock, core = _make_server(_BASE_PORT + 9, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 9)
            await _enter_buffering(reader, writer)

            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)  # ok for M2
            await asyncio.sleep(0.05)

            assert core.state in (ProxyState.EXECUTING, ProxyState.PASSTHROUGH)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_m30_finalizes_job(self, tmp_path):
        """M30 command exits Buffering and starts execution (EXECUTING or Passthrough)."""
        server, mock, core = _make_server(_BASE_PORT + 10, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 10)
            await _enter_buffering(reader, writer)

            writer.write(b"M30\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            await asyncio.sleep(0.05)

            assert core.state in (ProxyState.EXECUTING, ProxyState.PASSTHROUGH)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_idle_timeout_finalizes_job(self, tmp_path):
        """No new lines for idle_timeout_s → job exits Buffering (EXECUTING or Passthrough)."""
        server, mock, core = _make_server(
            _BASE_PORT + 11, tmp_path / "jobs", idle_timeout_s=0.1
        )
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 11)
            await _enter_buffering(reader, writer)

            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            # Wait longer than idle_timeout_s (0.1s)
            await asyncio.sleep(0.35)

            assert core.state in (ProxyState.EXECUTING, ProxyState.PASSTHROUGH)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_disconnect_mid_buffer_discards_file(self, tmp_path):
        """TCP disconnect while buffering discards the job file."""
        jobs_dir = tmp_path / "jobs"
        server, mock, core = _make_server(_BASE_PORT + 12, jobs_dir)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 12)
            await _enter_buffering(reader, writer)

            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            # Abruptly close connection
            writer.close()
            await writer.wait_closed()

            # Give server time to detect EOF and run on_client_disconnected
            await asyncio.sleep(0.15)
        finally:
            await server.stop()

        assert core.state == ProxyState.DISCONNECTED
        # The job file should not exist (discarded)
        assert not (jobs_dir / "current.gcode").exists()

    async def test_buffered_file_correct_content(self, tmp_path):
        """Lines sent during buffering appear in the output file."""
        jobs_dir = tmp_path / "jobs"
        server, mock, core = _make_server(_BASE_PORT + 13, jobs_dir)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 13)
            await _enter_buffering(reader, writer)

            for cmd in [b"G0 X10\n", b"G1 Y20\n", b"G1 Y30\n"]:
                writer.write(cmd)
            await writer.drain()
            for _ in range(3):
                await asyncio.wait_for(reader.readline(), timeout=2.0)

            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            await asyncio.sleep(0.05)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        # Phase 5: after finalize the file is timestamped, not current.gcode
        gcode_files = list(jobs_dir.glob("*.gcode"))
        assert len(gcode_files) == 1, f"Expected 1 gcode file, got {gcode_files}"
        content = gcode_files[0].read_text()
        assert "G0 X10\n" in content
        assert "G1 Y20\n" in content
        assert "G1 Y30\n" in content


# ---------------------------------------------------------------------------
# TestHeuristicTrigger — integration: burst of motion commands triggers buffering
# ---------------------------------------------------------------------------


class TestHeuristicTrigger:
    async def test_burst_does_not_trigger_buffering(self, tmp_path):
        """Rapid burst of motion commands (framing/jogging) stays in Passthrough.

        The heuristic auto-detect is disabled by default because framing in
        LightBurn produces the same burst pattern as a real job start, making it
        unreliable. Only the explicit G4 P0.0 marker triggers buffering.
        """
        server, mock, core = _make_server(_BASE_PORT + 14, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 14)

            # Send 5 motion commands fast — same pattern as framing
            for i in range(5):
                writer.write(f"G0 X{i}\n".encode())
            await writer.drain()

            for _ in range(5):
                await asyncio.wait_for(reader.readline(), timeout=2.0)

            await asyncio.sleep(0.05)
            # Must stay in Passthrough — framing should not be buffered
            assert core.state == ProxyState.PASSTHROUGH

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_only_explicit_marker_triggers_buffering(self, tmp_path):
        """Buffering only starts when the explicit start marker is received."""
        server, mock, core = _make_server(_BASE_PORT + 15, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 15)

            # Burst of motion commands — no trigger
            for i in range(10):
                writer.write(f"G0 X{i}\n".encode())
            await writer.drain()
            for _ in range(10):
                await asyncio.wait_for(reader.readline(), timeout=2.0)

            assert core.state == ProxyState.PASSTHROUGH

            # Now send the explicit marker — this should trigger
            await _enter_buffering(reader, writer)
            assert core.state == ProxyState.BUFFERING

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_marker_normalization(self, tmp_path):
        """G-code marker matching is case- and whitespace-insensitive.

        LightBurn may send 'g4 p0.0' or 'G04 P0.0' — all should trigger buffering.
        """
        for variant, port_offset in [(b"g4 p0.0\n", 17), (b"G04 P0.0\n", 18)]:
            server, mock, core = _make_server(
                _BASE_PORT + port_offset, tmp_path / f"jobs{port_offset}"
            )
            await server.start()
            try:
                reader, writer = await _connect(_BASE_PORT + port_offset)
                writer.write(variant)
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=2.0)
                await asyncio.sleep(0.05)
                assert core.state == ProxyState.BUFFERING, f"{variant!r} did not trigger buffering"
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()


# ---------------------------------------------------------------------------
# TestPhase1Regression — TcpServer without proxy_core behaves as pure passthrough
# ---------------------------------------------------------------------------


class TestPhase1Regression:
    async def test_no_proxy_core_pure_passthrough(self):
        """Without proxy_core, TcpServer is a transparent relay (Phase 1)."""
        mock = MockSerialConnection(auto_respond=True)
        server = TcpServer("127.0.0.1", _BASE_PORT + 16, mock)  # no proxy_core
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 16)
            writer.write(b"$H\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert response == b"ok\n"
        assert "$H" in mock.last_sent_lines()
