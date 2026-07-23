"""Phase 1 test suite.

Tests cover:
- grbl_protocol.py pure functions
- config.py loading and defaults
- TcpServer relay via MockSerialConnection (no hardware required)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from grbl_proxy import grbl_protocol
from grbl_proxy.config import AutoDetectConfig, Config, JobConfig, SerialConfig, TcpConfig, load_config, resolve_serial_port
from grbl_proxy.proxy_core import ProxyCore
from grbl_proxy.tcp_server import TcpServer
from tests.mock_grbl import MockSerialConnection

# ---------------------------------------------------------------------------
# grbl_protocol tests (synchronous)
# ---------------------------------------------------------------------------


class TestIsStatusReport:
    def test_idle(self):
        assert grbl_protocol.is_status_report("<Idle|MPos:0.000,0.000,0.000|FS:0,0>")

    def test_run(self):
        assert grbl_protocol.is_status_report("<Run|MPos:1.000,2.000,0.000|FS:3000,255>")

    def test_ok_is_not_status(self):
        assert not grbl_protocol.is_status_report("ok")

    def test_error_is_not_status(self):
        assert not grbl_protocol.is_status_report("error:2")

    def test_empty_is_not_status(self):
        assert not grbl_protocol.is_status_report("")


class TestParseStatusReport:
    def test_idle(self):
        r = grbl_protocol.parse_status_report("<Idle|MPos:10.000,20.000,0.000|FS:0,0>")
        assert r is not None
        assert r["state"] == "Idle"
        assert r["mpos"] == (10.0, 20.0, 0.0)
        assert r["fs"] == (0, 0)

    def test_run_with_feed(self):
        r = grbl_protocol.parse_status_report("<Run|MPos:1.500,2.300,0.000|FS:3000,255>")
        assert r is not None
        assert r["state"] == "Run"
        assert r["mpos"] == (1.5, 2.3, 0.0)
        assert r["fs"] == (3000, 255)

    def test_with_wco(self):
        r = grbl_protocol.parse_status_report(
            "<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:1.000,2.000,0.000>"
        )
        assert r is not None
        assert r["wco"] == (1.0, 2.0, 0.0)

    def test_with_crlf(self):
        r = grbl_protocol.parse_status_report(
            "<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n"
        )
        assert r is not None
        assert r["state"] == "Idle"

    def test_invalid_returns_none(self):
        assert grbl_protocol.parse_status_report("ok") is None
        assert grbl_protocol.parse_status_report("") is None
        assert grbl_protocol.parse_status_report("not a status") is None

    def test_negative_coords(self):
        r = grbl_protocol.parse_status_report("<Idle|MPos:-5.000,-10.500,0.000|FS:0,0>")
        assert r is not None
        assert r["mpos"] == (-5.0, -10.5, 0.0)


class TestIsOkErrorAlarm:
    def test_ok(self):
        assert grbl_protocol.is_ok("ok")
        assert grbl_protocol.is_ok("ok\n")
        assert grbl_protocol.is_ok("ok\r\n")
        assert not grbl_protocol.is_ok("ok extra")
        assert not grbl_protocol.is_ok("error:2")

    def test_error(self):
        assert grbl_protocol.is_error("error:2")
        assert grbl_protocol.is_error("error:2\n")
        assert not grbl_protocol.is_error("ok")

    def test_alarm(self):
        assert grbl_protocol.is_alarm("ALARM:1")
        assert grbl_protocol.is_alarm("ALARM:1\n")
        assert not grbl_protocol.is_alarm("ok")

    def test_get_error_code(self):
        assert grbl_protocol.get_error_code("error:2") == 2
        assert grbl_protocol.get_error_code("error:22") == 22
        assert grbl_protocol.get_error_code("ok") is None

    def test_get_alarm_code(self):
        assert grbl_protocol.get_alarm_code("ALARM:3") == 3
        assert grbl_protocol.get_alarm_code("ok") is None


class TestIsMotionCommand:
    def test_g0(self):
        assert grbl_protocol.is_motion_command("G0 X10 Y20")
        assert grbl_protocol.is_motion_command("g0 x10 y20")  # lowercase

    def test_g1(self):
        assert grbl_protocol.is_motion_command("G1 X5 F1000")

    def test_m3(self):
        assert grbl_protocol.is_motion_command("M3 S255")

    def test_query_is_not_motion(self):
        assert not grbl_protocol.is_motion_command("?")
        assert not grbl_protocol.is_motion_command("$$")
        assert not grbl_protocol.is_motion_command("$H")


class TestMakeStatusResponse:
    def test_default(self):
        r = grbl_protocol.make_status_response()
        assert r.startswith("<Idle|")
        assert "MPos:0.000,0.000,0.000" in r
        assert r.endswith("\n")

    def test_custom(self):
        r = grbl_protocol.make_status_response("Run", (1.5, 2.0, 0.0), 3000, 255)
        assert "<Run|" in r
        assert "MPos:1.500,2.000,0.000" in r
        assert "FS:3000,255" in r


# ---------------------------------------------------------------------------
# config.py tests
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_defaults_when_no_file(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg.tcp.port == 8899
        assert cfg.serial.port == "auto"
        assert cfg.serial.dtr is False

    def test_partial_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("tcp:\n  port: 1234\n")
        cfg = load_config(cfg_file)
        assert cfg.tcp.port == 1234
        assert cfg.serial.port == "auto"  # default preserved

    def test_serial_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("serial:\n  port: /dev/ttyUSB1\n  baud: 9600\n")
        cfg = load_config(cfg_file)
        assert cfg.serial.port == "/dev/ttyUSB1"
        assert cfg.serial.baud == 9600

    def test_invalid_yaml_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(": invalid: yaml: {")
        with pytest.raises(ValueError, match="Invalid YAML"):
            load_config(cfg_file)


class TestResolveSerialPort:
    def test_explicit_port_returned_as_is(self):
        cfg = SerialConfig(port="/dev/ttyUSB0")
        assert resolve_serial_port(cfg) == "/dev/ttyUSB0"

    def test_auto_single_device(self):
        cfg = SerialConfig(port="auto")
        with patch("glob.glob", side_effect=lambda p: ["/dev/ttyUSB0"] if "USB" in p else []):
            result = resolve_serial_port(cfg)
        assert result == "/dev/ttyUSB0"

    def test_auto_no_devices_falls_back(self):
        cfg = SerialConfig(port="auto")
        with patch("glob.glob", return_value=[]):
            result = resolve_serial_port(cfg)
        assert result == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# TcpServer + MockSerialConnection relay tests (async)
# ---------------------------------------------------------------------------

# Use an ephemeral high port range to avoid conflicts with other tests
_BASE_PORT = 19900


@pytest.fixture
def mock_serial():
    return MockSerialConnection()


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


class TestTcpRelay:
    async def test_command_forwarded_to_serial(self, mock_serial):
        """A command sent via TCP arrives at the serial mock."""
        server = TcpServer("127.0.0.1", _BASE_PORT + 1, mock_serial)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 1)
            writer.write(b"$H\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert response == b"ok\r\n"
        assert "$H" in mock_serial.last_sent_lines()

    async def test_status_query_response(self, mock_serial):
        """A '?' query gets a synthesized status response."""
        server = TcpServer("127.0.0.1", _BASE_PORT + 2, mock_serial)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 2)
            writer.write(b"?\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert response.startswith(b"<")
        assert b"MPos:" in response

    async def test_serial_response_forwarded_to_tcp(self, mock_serial):
        """Responses injected into serial mock arrive at the TCP client."""
        mock_serial_no_auto = MockSerialConnection(auto_respond=False)
        server = TcpServer("127.0.0.1", _BASE_PORT + 3, mock_serial_no_auto)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 3)
            # Inject a response before sending a command
            mock_serial_no_auto.inject("ok")
            writer.write(b"G0 X10\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        # Must be CR+LF terminated to match a real GRBL device: read_line()
        # strips whatever the firmware sent, and a bare "\n" back to LightBurn
        # can leave a strict parser waiting on an "incomplete" line (BUSY lock).
        assert response == b"ok\r\n"
        assert response.endswith(b"\r\n")

    async def test_new_client_drops_old(self, mock_serial):
        """Connecting a second client causes the first to get EOF."""
        server = TcpServer("127.0.0.1", _BASE_PORT + 4, mock_serial)
        await server.start()
        try:
            reader1, writer1 = await _connect(_BASE_PORT + 4)
            # Give the server time to register the first connection
            await asyncio.sleep(0.05)

            reader2, writer2 = await _connect(_BASE_PORT + 4)
            # Give the server time to drop the first and register the second
            await asyncio.sleep(0.1)

            # First connection should be closed — reading should return EOF quickly
            try:
                data = await asyncio.wait_for(reader1.read(100), timeout=1.0)
                assert data == b"" or data == b"error:9\r\n"
            except asyncio.TimeoutError:
                pytest.fail("Old connection was not dropped within 1s")

            writer2.close()
            await writer2.wait_closed()
            try:
                writer1.close()
            except Exception:
                pass
        finally:
            await server.stop()

    async def test_multiple_commands_in_order(self, mock_serial):
        """Multiple sequential commands get sequential ok responses."""
        server = TcpServer("127.0.0.1", _BASE_PORT + 5, mock_serial)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 5)

            commands = [b"G0 X0\n", b"G0 X10\n", b"G0 X20\n"]
            for cmd in commands:
                writer.write(cmd)
            await writer.drain()

            responses = []
            for _ in commands:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                responses.append(line)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert all(r == b"ok\r\n" for r in responses)

    async def test_alarm_forwarded(self, mock_serial):
        """ALARM responses from serial are forwarded to LightBurn."""
        mock_no_auto = MockSerialConnection(auto_respond=False)
        server = TcpServer("127.0.0.1", _BASE_PORT + 6, mock_no_auto)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 6)
            mock_no_auto.inject("ALARM:1")
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert line == b"ALARM:1\r\n"

    async def test_status_report_forwarded_verbatim(self, mock_serial):
        """Status reports reach LightBurn unmodified — the proxy does not
        rewrite GRBL's fields. Only the line terminator is normalised to CR+LF.

        Bf is firmware-specific (the Falcon 2 Pro reports Bf:127,65535 — a
        128-block planner and an unbounded 0xFFFF RX buffer) but is passed
        through as-is: rewriting the wire protocol risks new breakage, and Bf
        was not the cause of the LightBurn stall.
        """
        mock_no_auto = MockSerialConnection(auto_respond=False)
        server = TcpServer("127.0.0.1", _BASE_PORT + 7, mock_no_auto)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 7)
            mock_no_auto.inject("<Idle|MPos:0.025,0.025,0.000|Bf:127,65535|FS:0,0>")
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

        assert line == b"<Idle|MPos:0.025,0.025,0.000|Bf:127,65535|FS:0,0>\r\n"


# ---------------------------------------------------------------------------
# Shutdown / SIGINT regression tests
#
# These tests guard against a previous bug where server.stop() (the SIGINT
# path) would hang indefinitely when a client was connected. The root cause
# was _serial_to_tcp blocking in asyncio.to_thread(serial.readline) with no
# way to be interrupted — the CancelledError from task.cancel() was swallowed
# by leaked ensure_future tasks, preventing the event loop from exiting.
#
# Each test asserts that server.stop() completes within 2 seconds. If the bug
# regresses the test hangs and pytest's own timeout (or asyncio.wait_for) will
# report a TimeoutError rather than a silent hang.
# ---------------------------------------------------------------------------

_STOP_TIMEOUT = 2.0  # seconds — stop() must complete within this time


class TestShutdownWithClientConnected:
    async def test_stop_while_client_connected_no_proxy(self):
        """server.stop() completes promptly with an active client (no ProxyCore).

        Regression: _serial_to_tcp was stuck in read_line() with no interrupt
        path, so stop() would hang until the 1-second readline timeout fired —
        and with multiple leaked tasks it never returned at all.
        """
        mock = MockSerialConnection(auto_respond=True)
        server = TcpServer("127.0.0.1", _BASE_PORT + 7, mock)
        await server.start()

        reader, writer = await _connect(_BASE_PORT + 7)
        # Exchange a round-trip to confirm the relay is running
        writer.write(b"?\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=2.0)

        # This must complete quickly — if it hangs the bug has regressed
        await asyncio.wait_for(server.stop(), timeout=_STOP_TIMEOUT)

        try:
            writer.close()
        except Exception:
            pass

    async def test_stop_while_client_idle_no_proxy(self):
        """server.stop() completes promptly when client is connected but silent.

        _serial_to_tcp was blocked waiting for serial data that never arrives
        (no ? polling from client). stop() must still cancel it cleanly.
        """
        mock = MockSerialConnection(auto_respond=False)
        server = TcpServer("127.0.0.1", _BASE_PORT + 8, mock)
        await server.start()

        reader, writer = await _connect(_BASE_PORT + 8)
        # Do NOT send any commands — _serial_to_tcp is blocked in read_line()
        await asyncio.sleep(0.05)

        await asyncio.wait_for(server.stop(), timeout=_STOP_TIMEOUT)

        try:
            writer.close()
        except Exception:
            pass

    async def test_stop_while_client_connected_with_proxy(self, tmp_path):
        """server.stop() completes promptly with ProxyCore attached (Phase 2 path).

        The Phase 2 routing path creates additional tasks per loop iteration.
        Verifies CancelledError propagates correctly through the stop_relay
        event mechanism even with ProxyCore's byte-routing active.
        """
        cfg = JobConfig(
            storage_dir=str(tmp_path / "jobs"),
            auto_detect=AutoDetectConfig(enabled=False),
        )
        mock = MockSerialConnection(auto_respond=True)
        core = ProxyCore(cfg, idle_timeout_s=0.1)
        server = TcpServer("127.0.0.1", _BASE_PORT + 9, mock, proxy_core=core)
        await server.start()

        reader, writer = await _connect(_BASE_PORT + 9)
        # Exchange several round-trips to ensure relay tasks are deep in their loop
        for _ in range(3):
            writer.write(b"?\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

        await asyncio.wait_for(server.stop(), timeout=_STOP_TIMEOUT)

        try:
            writer.close()
        except Exception:
            pass

    async def test_stop_during_rapid_polling(self):
        """server.stop() completes during high-frequency ? polling.

        Sends ? queries back-to-back before calling stop(), maximising the
        chance that _serial_to_tcp is mid-wait when cancellation arrives.
        """
        mock = MockSerialConnection(auto_respond=True)
        server = TcpServer("127.0.0.1", _BASE_PORT + 10, mock)
        await server.start()

        reader, writer = await _connect(_BASE_PORT + 10)

        # Flood the relay with status queries concurrently with stop()
        async def _poll():
            try:
                while True:
                    writer.write(b"?\n")
                    await writer.drain()
                    await asyncio.sleep(0.01)
            except Exception:
                pass

        poll_task = asyncio.create_task(_poll())
        await asyncio.sleep(0.1)  # let polling run for a bit

        await asyncio.wait_for(server.stop(), timeout=_STOP_TIMEOUT)

        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)
        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Passthrough stall watchdog
#
# Regression / diagnostic guard: when GRBL stops responding mid-command in
# Passthrough (observed on hardware — a frame that acked 3 of 11 lines then
# went silent), the relay's read loop keeps ticking on READLINE_TIMEOUT with
# nothing to forward. The watchdog must surface that ONCE, with proof the read
# loop is alive, so a controller-side halt is distinguishable from a proxy
# read-path wedge. It must NOT fire on a merely-idle link (nothing written).
# ---------------------------------------------------------------------------


class TestPassthroughStallWatchdog:
    async def test_warns_once_when_grbl_goes_silent_after_a_write(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging

        import grbl_proxy.tcp_server as tcp_server_mod

        # Shrink the threshold so a single post-write timeout tick trips it.
        monkeypatch.setattr(tcp_server_mod, "PASSTHROUGH_STALL_WARN_S", 0.0)

        cfg = JobConfig(
            storage_dir=str(tmp_path / "jobs"),
            auto_detect=AutoDetectConfig(enabled=False),
        )
        # auto_respond=False → the mock never acks: GRBL "goes silent".
        mock = MockSerialConnection(auto_respond=False)
        core = ProxyCore(cfg, idle_timeout_s=5.0)
        server = TcpServer("127.0.0.1", _BASE_PORT + 20, mock, proxy_core=core)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 20)
            # Push a command toward GRBL — stamps last_write_at. No ok comes back.
            writer.write(b"G0 X1\n")
            await writer.drain()

            with caplog.at_level(logging.WARNING, logger="grbl_proxy.tcp_server"):
                # Wait past a couple of 1s readline timeout ticks.
                await asyncio.sleep(2.3)

            stalls = [
                r for r in caplog.records if "Passthrough stall" in r.getMessage()
            ]
            assert len(stalls) == 1, f"expected exactly one stall warning, got {len(stalls)}"
            assert "read loop ALIVE" in stalls[0].getMessage()

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_no_warning_when_idle_with_nothing_written(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging

        import grbl_proxy.tcp_server as tcp_server_mod

        monkeypatch.setattr(tcp_server_mod, "PASSTHROUGH_STALL_WARN_S", 0.0)

        cfg = JobConfig(
            storage_dir=str(tmp_path / "jobs"),
            auto_detect=AutoDetectConfig(enabled=False),
        )
        mock = MockSerialConnection(auto_respond=False)
        core = ProxyCore(cfg, idle_timeout_s=5.0)
        server = TcpServer("127.0.0.1", _BASE_PORT + 21, mock, proxy_core=core)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 21)
            # Connect but send nothing — the link is idle, not stalled.
            with caplog.at_level(logging.WARNING, logger="grbl_proxy.tcp_server"):
                await asyncio.sleep(2.3)

            stalls = [
                r for r in caplog.records if "Passthrough stall" in r.getMessage()
            ]
            assert stalls == [], "watchdog must not fire on an idle link"

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# Passthrough write coalescing
#
# A command burst that LightBurn delivers in one TCP chunk must reach GRBL as a
# SINGLE contiguous serial write, not one write per line. Per-line writes space
# commands out enough to shift GRBL's execution timing (observed as the laser
# module's airflow spin-up window collapsing after M3 on the Falcon 2), a
# difference a direct serial connection never exhibits.
# ---------------------------------------------------------------------------


class _NullWriter:
    """Minimal StreamWriter stand-in for _route_bytes unit tests."""

    def write(self, data: bytes) -> None:  # noqa: D401
        pass

    async def drain(self) -> None:
        pass


class TestPassthroughWriteCoalescing:
    async def test_burst_becomes_single_serial_write(self, tmp_path):
        cfg = JobConfig(
            storage_dir=str(tmp_path / "jobs"),
            auto_detect=AutoDetectConfig(enabled=False),
        )
        mock = MockSerialConnection(auto_respond=False)
        core = ProxyCore(cfg, idle_timeout_s=5.0)
        core.on_client_connected()  # Disconnected → Passthrough
        server = TcpServer("127.0.0.1", _BASE_PORT + 22, mock, proxy_core=core)

        # One TCP chunk carrying a multi-line frame-style burst, including an
        # inline "?" realtime query (GRBL plucks it; it must still coalesce).
        burst = b"G0 X95Y92\nM3\nG1 Y304S10F6000\n?\nG1 X287\n"
        await server._route_bytes(burst, _NullWriter())

        # Exactly one serial write, and it carries every forwarded byte in order.
        assert len(mock.tx_log) == 1, f"expected 1 coalesced write, got {len(mock.tx_log)}"
        assert mock.tx_log[0] == b"G0 X95Y92\nM3\nG1 Y304S10F6000\n?\nG1 X287\n"

    async def test_partial_trailing_line_is_held_until_terminated(self, tmp_path):
        cfg = JobConfig(
            storage_dir=str(tmp_path / "jobs"),
            auto_detect=AutoDetectConfig(enabled=False),
        )
        mock = MockSerialConnection(auto_respond=False)
        core = ProxyCore(cfg, idle_timeout_s=5.0)
        core.on_client_connected()
        server = TcpServer("127.0.0.1", _BASE_PORT + 23, mock, proxy_core=core)

        # First chunk ends mid-line: only the complete line flushes; the tail
        # stays buffered (a partial line must never be written to GRBL).
        await server._route_bytes(b"G0 X1\nG1 X2", _NullWriter())
        assert mock.tx_log == [b"G0 X1\n"]

        # Second chunk completes the held line.
        await server._route_bytes(b"3\n", _NullWriter())
        assert mock.tx_log == [b"G0 X1\n", b"G1 X23\n"]
