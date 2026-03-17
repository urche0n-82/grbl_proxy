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
from grbl_proxy.config import Config, SerialConfig, TcpConfig, load_config, resolve_serial_port
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

        assert response == b"ok\n"
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

        assert response == b"ok\n"

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
                assert data == b"" or data == b"error:9\n"
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

        assert all(r == b"ok\n" for r in responses)

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

        assert line == b"ALARM:1\n"
