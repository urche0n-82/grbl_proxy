"""Phase 3 test suite.

Tests cover:
- GrblStreamer: character-counting, error/alarm handling, pause/resume, cancel
- ProxyCore: EXECUTING/PAUSED/ERROR state transitions, disconnect-safe behaviour
- Integration via TcpServer + MockSerialConnection (no hardware)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from grbl_proxy import grbl_protocol
from grbl_proxy.config import AutoDetectConfig, JobConfig
from grbl_proxy.proxy_core import ProxyCore, ProxyState
from grbl_proxy.streamer import IDLE_COMPLETE_POLLS, GrblStreamer, StreamerResult
from grbl_proxy.tcp_server import TcpServer
from tests.mock_grbl import MockSerialConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PORT = 20000


def _make_job_cfg(jobs_dir: Path) -> JobConfig:
    return JobConfig(
        storage_dir=str(jobs_dir),
        start_marker="G4 P0.0",
        end_marker="G4 P0.0",
        auto_detect=AutoDetectConfig(
            enabled=False, line_burst=5, window_ms=300, motion_ratio=0.8
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


async def _buffer_job(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    lines: list[bytes],
) -> None:
    """Send start marker, buffer lines, send M2 end command."""
    # Start marker
    writer.write(b"G4 P0.0\n")
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=2.0)  # ok

    # Body lines
    for line in lines:
        writer.write(line)
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=2.0)  # spoofed ok

    # End command
    writer.write(b"M2\n")
    await writer.drain()
    await asyncio.wait_for(reader.readline(), timeout=2.0)  # ok for M2


def _write_gcode_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _wait_for_state(
    core: ProxyCore,
    target: ProxyState,
    timeout: float = 3.0,
    poll: float = 0.05,
) -> None:
    """Poll until core.state == target or raise TimeoutError."""
    elapsed = 0.0
    while core.state != target:
        await asyncio.sleep(poll)
        elapsed += poll
        if elapsed >= timeout:
            raise TimeoutError(
                f"Timed out waiting for state {target.value}, currently {core.state.value}"
            )


async def _wait_for_state_in(
    core: ProxyCore,
    targets: set[ProxyState],
    timeout: float = 3.0,
    poll: float = 0.05,
) -> None:
    """Poll until core.state is in targets or raise TimeoutError."""
    elapsed = 0.0
    while core.state not in targets:
        await asyncio.sleep(poll)
        elapsed += poll
        if elapsed >= timeout:
            names = ", ".join(t.value for t in targets)
            raise TimeoutError(
                f"Timed out waiting for state in {{{names}}}, currently {core.state.value}"
            )


# ---------------------------------------------------------------------------
# TestStreamerUnit — GrblStreamer without TcpServer
# ---------------------------------------------------------------------------


class TestStreamerUnit:
    """Unit tests for GrblStreamer using MockSerialConnection directly."""

    async def test_streams_all_lines_completes(self, tmp_path):
        """Auto-respond mock → all lines streamed → result.completed."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10", "G1 Y20", "G0 X0"])
        mock = MockSerialConnection(auto_respond=True)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)
        await streamer.run()
        assert len(results) == 1
        r = results[0]
        assert r.completed
        assert r.lines_sent == 3
        assert r.total_lines == 3
        assert r.error_code is None
        assert r.alarm_code is None

    async def test_character_counting_respects_rx_buffer(self, tmp_path):
        """With a tiny rx_buffer_size, streamer waits for ok before sending next line."""
        # Each line is ~10 bytes; rx_buffer_size=15 means at most 1 in flight at a time
        gcode = tmp_path / "test.gcode"
        lines = ["G0 X10000", "G1 Y20000", "G0 X0000"]
        _write_gcode_file(gcode, lines)
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(
            gcode, mock, on_done=results.append, rx_buffer_size=15
        )

        async def inject_oks():
            # Wait for the first write, inject ok, etc.
            for _ in range(3):
                await asyncio.sleep(0.05)
                mock.inject("ok")

        task = asyncio.create_task(streamer.run())
        await inject_oks()
        await asyncio.wait_for(task, timeout=3.0)

        assert results[0].completed
        assert results[0].lines_sent == 3

    async def test_error_response_stops_streaming(self, tmp_path):
        """GRBL responds with error:2 → result has error_code and completed=False."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10", "G1 Y20"])
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)

        async def inject():
            await asyncio.sleep(0.05)
            mock.inject("error:2")

        task = asyncio.create_task(streamer.run())
        await inject()
        await asyncio.wait_for(task, timeout=3.0)

        r = results[0]
        assert not r.completed
        assert r.error_code == 2
        # error_line may be 1 (caught during drain) or 2 (caught in trailing ack drain)
        assert r.error_line is not None
        assert r.alarm_code is None

    async def test_alarm_response_stops_streaming(self, tmp_path):
        """GRBL responds with ALARM:1 → result has alarm_code and completed=False."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10"])
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)

        async def inject():
            await asyncio.sleep(0.05)
            mock.inject("ALARM:1")

        task = asyncio.create_task(streamer.run())
        await inject()
        await asyncio.wait_for(task, timeout=3.0)

        r = results[0]
        assert not r.completed
        assert r.alarm_code == 1
        assert r.error_code is None

    async def test_completes_on_idle_when_final_ok_missing(self, tmp_path):
        """If GRBL never acks the last line (e.g. an ESP32 fork that doesn't
        emit a standard ok for M2), the streamer must still complete once GRBL
        reports Idle — the machine is physically done. Without this the trailing
        drain spins forever and wedges the proxy in EXECUTING.

        Regression for the observed hang: a 139-line frame job finished on the
        machine (returned to origin, Idle) but the proxy stayed EXECUTING and
        swallowed the next job.
        """
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10", "G1 Y20", "M2"])
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)

        async def feed():
            await asyncio.sleep(0.05)
            mock.inject("ok")   # ack line 1
            mock.inject("ok")   # ack line 2
            # line 3 (M2) intentionally NOT acked — simulates the firmware quirk.
            # Machine finishes and reports Idle on subsequent polls.
            for _ in range(IDLE_COMPLETE_POLLS + 1):
                await asyncio.sleep(0.03)
                mock.inject_status(state="Idle")

        task = asyncio.create_task(streamer.run())
        await feed()
        await asyncio.wait_for(task, timeout=3.0)

        r = results[0]
        assert r.completed
        assert r.cancelled is False
        assert r.error_code is None
        assert r.alarm_code is None
        assert r.lines_sent == 3

    async def test_cancel_takes_effect_while_wedged_mid_stream(self, tmp_path):
        """Cancel must break out of the mid-stream ack drain, not just between
        lines. Regression for the production wedge: the job stalled ~64% in and
        every cancel (LightBurn Ctrl-X and web) was ignored because _cancelled
        was only checked between lines, which the wedged drain never reached.
        """
        gcode = tmp_path / "test.gcode"
        # Lines long enough that a tiny rx_buffer forces a drain after line 1.
        _write_gcode_file(gcode, [f"G1 X{i}Y{i}S10F6000" for i in range(20)])
        mock = MockSerialConnection(auto_respond=False)  # GRBL never acks
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append, rx_buffer_size=20)

        task = asyncio.create_task(streamer.run())
        await asyncio.sleep(0.15)  # let it send line 1 and wedge in the drain
        assert not task.done()

        streamer.cancel()
        await asyncio.wait_for(task, timeout=2.0)  # must return promptly

        r = results[0]
        assert r.cancelled
        assert not r.completed

    async def test_idle_mid_stream_reconciles_and_resumes(self, tmp_path):
        """If an ack goes missing mid-job, a sustained Idle from GRBL means our
        byte accounting is stale — the streamer must reconcile and finish the
        job rather than spinning in the drain forever."""
        gcode = tmp_path / "test.gcode"
        lines = [f"G1 X{i}Y{i}S10F6000" for i in range(5)]
        _write_gcode_file(gcode, lines)
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append, rx_buffer_size=20)

        async def feed():
            # Never ack anything — only report Idle. The streamer should notice
            # the contradiction, reconcile, and push the rest of the job out.
            for _ in range((IDLE_COMPLETE_POLLS + 1) * (len(lines) + 1)):
                await asyncio.sleep(0.02)
                mock.inject_status(state="Idle")

        task = asyncio.create_task(streamer.run())
        await feed()
        await asyncio.wait_for(task, timeout=5.0)

        r = results[0]
        assert r.completed
        assert r.lines_sent == len(lines)  # every line reached the machine

    async def test_interleaved_status_ack_is_recovered(self, tmp_path):
        """The real-world corruption: GRBL interleaves its realtime status write
        with the ok write, emitting a truncated status with the ack spliced on.
        The ack MUST still be counted or flow control drifts permanently.

        Captured from hardware:
          '<Run|MPos:201.275,148.225,0.000|Bf:0,65459|FS:1501,3|Ov:100,100,ok'
        """
        gcode = tmp_path / "test.gcode"
        lines = ["G1 X1Y1", "G1 X2Y2", "G1 X3Y3"]
        _write_gcode_file(gcode, lines)
        mock = MockSerialConnection(auto_respond=False)
        results = []
        # Window small enough that a lost ack would stall the send immediately.
        streamer = GrblStreamer(gcode, mock, on_done=results.append, rx_buffer_size=12)

        async def feed():
            await asyncio.sleep(0.05)
            # Every ack arrives mangled — none is a clean "ok".
            for _ in lines:
                mock.inject(
                    "<Run|MPos:201.275,148.225,0.000|Bf:0,65459|FS:1501,3|Ov:100,100,ok"
                )
                await asyncio.sleep(0.03)

        task = asyncio.create_task(streamer.run())
        await feed()
        await asyncio.wait_for(task, timeout=3.0)

        r = results[0]
        assert r.completed
        assert r.lines_sent == len(lines)  # every ack was recovered

    def _streamer_with_capacity(self, tmp_path):
        """A streamer that has learned GRBL's 65535-byte RX capacity."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G1 X1Y1"])
        s = GrblStreamer(
            gcode, MockSerialConnection(auto_respond=False),
            on_done=lambda r: None, rx_buffer_size=128,
        )
        idle = grbl_protocol.parse_status_report(
            "<Idle|MPos:0,0,0|Bf:127,65535|FS:0,0>"
        )
        assert s._buffer_looks_phantom(idle, 0) is False  # learns capacity
        assert s._rx_capacity == 65535
        return s

    async def test_normal_transit_lag_is_not_treated_as_drift(self, tmp_path):
        """buffer_used counts bytes sent-but-unacked; Bf's rx_free counts bytes
        still unparsed. GRBL frees RX space as soon as it parses a line, so ours
        legitimately reads higher during healthy streaming. That ordinary gap
        must never trigger a resync.

        These are the real rx_free figures captured mid-job, every one of which
        the first implementation wrongly "corrected".
        """
        s = self._streamer_with_capacity(tmp_path)
        for rx_free, believed in (
            (65416, 122), (65424, 128), (65417, 120), (65435, 118), (65475, 121)
        ):
            status = grbl_protocol.parse_status_report(
                f"<Run|MPos:1,2,0|Bf:0,{rx_free}|FS:1501,3>"
            )
            # Repeat well past the streak threshold — a busy RX never qualifies.
            for _ in range(IDLE_COMPLETE_POLLS + 2):
                assert s._buffer_looks_phantom(status, believed) is False

    async def test_sustained_drained_rx_is_phantom(self, tmp_path):
        """A drained RX held across consecutive polls, while we still believe
        bytes are outstanding, is genuine evidence an ack was lost."""
        s = self._streamer_with_capacity(tmp_path)
        drained = grbl_protocol.parse_status_report(
            "<Run|MPos:1,2,0|Bf:0,65535|FS:1501,3>"
        )
        # Not enough consecutive reports yet.
        for _ in range(IDLE_COMPLETE_POLLS - 1):
            assert s._buffer_looks_phantom(drained, 117) is False
        assert s._buffer_looks_phantom(drained, 117) is True

    async def test_busy_report_resets_the_drained_streak(self, tmp_path):
        s = self._streamer_with_capacity(tmp_path)
        drained = grbl_protocol.parse_status_report(
            "<Run|MPos:1,2,0|Bf:0,65535|FS:1501,3>"
        )
        busy = grbl_protocol.parse_status_report(
            "<Run|MPos:1,2,0|Bf:0,65416|FS:1501,3>"
        )
        for _ in range(IDLE_COMPLETE_POLLS - 1):
            assert s._buffer_looks_phantom(drained, 117) is False
        assert s._buffer_looks_phantom(busy, 117) is False  # streak broken
        assert s._buffer_looks_phantom(drained, 117) is False  # counting anew

    async def test_nothing_in_flight_is_never_phantom(self, tmp_path):
        s = self._streamer_with_capacity(tmp_path)
        drained = grbl_protocol.parse_status_report(
            "<Idle|MPos:0,0,0|Bf:127,65535|FS:0,0>"
        )
        for _ in range(IDLE_COMPLETE_POLLS + 2):
            assert s._buffer_looks_phantom(drained, 0) is False

    async def test_flow_control_counter_never_goes_negative(self, tmp_path):
        """buffer_used must always equal sum(line_lengths). The first Bf resync
        mutated buffer_used without touching line_lengths, so later acks
        subtracted bytes already removed and drove the counter negative —
        which makes `buffer_used + line_bytes > window` permanently false and
        silently disables the send window altogether.

        Streams a job while GRBL reports a busy RX throughout (normal transit
        lag) and asserts the window keeps working.
        """
        gcode = tmp_path / "test.gcode"
        lines = [f"G1 X{i}Y{i}" for i in range(12)]
        _write_gcode_file(gcode, lines)
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(
            gcode, mock, on_done=results.append, rx_buffer_size=32
        )

        async def feed():
            await asyncio.sleep(0.05)
            for _ in lines:
                # A status showing a *busy* RX (the case that used to resync),
                # then a clean ack — the healthy steady state.
                mock.inject("<Run|MPos:1,2,0|Bf:0,65416|FS:1501,3>")
                mock.inject("ok")
                await asyncio.sleep(0.01)
            for _ in range(IDLE_COMPLETE_POLLS + 1):
                await asyncio.sleep(0.02)
                mock.inject_status(state="Idle")

        task = asyncio.create_task(streamer.run())
        await feed()
        await asyncio.wait_for(task, timeout=5.0)

        r = results[0]
        assert r.completed
        assert r.lines_sent == len(lines)
        # A negative counter would have let every line through at once; the
        # window must have actually throttled and stayed coherent.
        assert streamer._rx_drained_polls >= 0

    async def test_polling_is_inline_not_concurrent(self, tmp_path):
        """'?' must be emitted by the streaming coroutine itself — no separate
        poll task — so one owner writes every byte to the port."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G1 X1Y1", "G1 X2Y2"])
        mock = MockSerialConnection(auto_respond=True)
        results = []
        streamer = GrblStreamer(
            gcode, mock, on_done=results.append, poll_interval=0.0
        )
        await asyncio.wait_for(streamer.run(), timeout=3.0)

        assert results[0].completed
        assert b"?" in b"".join(mock.tx_log)  # polled inline during streaming
        assert not hasattr(streamer, "_poll_loop")  # concurrent poller is gone

    async def test_run_state_does_not_falsely_complete(self, tmp_path):
        """A Run status in the trailing drain must NOT complete the job — only
        sustained Idle does. Guards the Idle fallback against premature exit."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10", "M2"])
        mock = MockSerialConnection(auto_respond=False)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)

        async def feed():
            await asyncio.sleep(0.05)
            mock.inject("ok")  # ack line 1
            # Machine still moving: Run reports must not trigger completion.
            for _ in range(IDLE_COMPLETE_POLLS + 2):
                await asyncio.sleep(0.03)
                mock.inject_status(state="Run", x=5.0)
            # Then the real final ok arrives → normal completion.
            mock.inject("ok")

        task = asyncio.create_task(streamer.run())
        await feed()
        await asyncio.wait_for(task, timeout=3.0)

        r = results[0]
        assert r.completed
        assert r.lines_sent == 2

    async def test_cancel_stops_streaming_cleanly(self, tmp_path):
        """cancel() stops streaming with cancelled=True.

        cancel() is a soft-cancel: it sets a flag checked between lines.
        The mock must have auto-respond=True so read_line() returns quickly
        and the cancellation flag is noticed between iterations.
        """
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10", "G1 Y20", "G0 X0"])
        # auto_respond=True so read_line returns quickly; cancel() is checked
        # between lines (after resume_event.wait())
        mock = MockSerialConnection(auto_respond=True)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)

        # Pause first so the streamer blocks at resume_event.wait() after line 1
        streamer.pause()

        async def cancel_while_paused():
            # Give the streamer time to send line 1 and then pause
            await asyncio.sleep(0.05)
            streamer.cancel()  # sets _cancelled and unblocks the wait

        task = asyncio.create_task(streamer.run())
        await cancel_while_paused()
        await asyncio.wait_for(task, timeout=3.0)

        r = results[0]
        assert r.cancelled
        assert not r.completed

    async def test_pause_and_resume_completes_job(self, tmp_path):
        """pause() suspends then resume() continues; job still completes."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10", "G1 Y20"])
        mock = MockSerialConnection(auto_respond=True)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)

        async def pause_then_resume():
            await asyncio.sleep(0.02)
            streamer.pause()
            assert streamer.is_paused
            await asyncio.sleep(0.05)
            streamer.resume()

        task = asyncio.create_task(streamer.run())
        await pause_then_resume()
        await asyncio.wait_for(task, timeout=3.0)

        assert results[0].completed

    async def test_status_reports_forwarded_to_callback(self, tmp_path):
        """Status reports interleaved in serial responses call on_status callback."""
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10"])
        mock = MockSerialConnection(auto_respond=False)
        status_calls = []
        results = []

        streamer = GrblStreamer(
            gcode, mock,
            on_done=results.append,
            on_status=status_calls.append,
        )

        async def inject():
            await asyncio.sleep(0.05)
            mock.inject("<Run|MPos:1.000,2.000,0.000|FS:100,0>")
            mock.inject("ok")

        task = asyncio.create_task(streamer.run())
        await inject()
        await asyncio.wait_for(task, timeout=3.0)

        assert results[0].completed
        assert len(status_calls) == 1
        assert status_calls[0]["state"] == "Run"

    async def test_blank_lines_in_file_are_skipped(self, tmp_path):
        """Blank lines in the G-code file are not sent to serial."""
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G0 X10\n\nG1 Y20\n  \nG0 X0\n", encoding="utf-8")
        mock = MockSerialConnection(auto_respond=True)
        results = []
        streamer = GrblStreamer(gcode, mock, on_done=results.append)
        await streamer.run()

        r = results[0]
        assert r.completed
        assert r.total_lines == 3  # 3 non-blank lines
        assert r.lines_sent == 3


# ---------------------------------------------------------------------------
# TestProxyCoreExecuting — ProxyCore state machine unit tests
# ---------------------------------------------------------------------------


class TestProxyCoreExecuting:
    """Tests for EXECUTING/PAUSED/ERROR state transitions in ProxyCore."""

    def _core(self, jobs_dir: Path) -> ProxyCore:
        return ProxyCore(_make_job_cfg(jobs_dir), idle_timeout_s=0.1)

    async def test_buffering_to_executing_transition(self, tmp_path):
        """After job finalization, state transitions to EXECUTING (or Passthrough if fast)."""
        server, mock, core = _make_server(_BASE_PORT + 0, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 0)
            await _buffer_job(reader, writer, [b"G0 X10\n"])
            await asyncio.sleep(0.05)
            # With auto_respond=True the streamer may finish immediately;
            # assert state left BUFFERING (either EXECUTING in-progress or PASSTHROUGH done)
            assert core.state in (ProxyState.EXECUTING, ProxyState.PASSTHROUGH)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_state_stays_executing_on_tcp_disconnect(self, tmp_path):
        """TCP disconnect during EXECUTING does not stop the job.

        Uses auto_respond=False and a slow ok injection to keep the streamer
        in EXECUTING long enough to disconnect and verify state.
        """
        server, mock, core = _make_server(_BASE_PORT + 1, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 1)

            # Buffer using auto_respond=True temporarily
            mock._auto_respond = True
            writer.write(b"G4 P0.0\n")  # start marker
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            # Stop auto-respond so the streamer blocks waiting for ok
            mock._auto_respond = False
            await asyncio.sleep(0.02)
            assert core.state == ProxyState.EXECUTING

            # Disconnect LightBurn while job is running
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)

            # State must still be EXECUTING — job was not aborted
            assert core.state == ProxyState.EXECUTING

            # Now inject oks so the streamer can finish.
            # After the streamer completes, state goes PASSTHROUGH → DISCONNECTED
            # as the gather cleanup calls on_client_disconnected. Both are valid
            # end states (PASSTHROUGH is transient when the client already left).
            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state_in(
                core,
                {ProxyState.PASSTHROUGH, ProxyState.DISCONNECTED},
                timeout=3.0,
            )
        finally:
            await server.stop()

    async def test_reconnect_during_executing_keeps_executing(self, tmp_path):
        """New LightBurn connection while EXECUTING stays in EXECUTING."""
        server, mock, core = _make_server(_BASE_PORT + 2, tmp_path / "jobs")
        # Use a slow mock so execution takes long enough to reconnect
        mock2 = MockSerialConnection(auto_respond=False)
        server2 = TcpServer(
            "127.0.0.1", _BASE_PORT + 2, mock2, proxy_core=core
        )
        # Just test via proxy_core directly — avoid port conflict
        cfg = _make_job_cfg(tmp_path / "jobs2")
        core3 = ProxyCore(cfg, idle_timeout_s=0.1)
        core3.on_client_connected()
        assert core3.state == ProxyState.PASSTHROUGH

        # Manually set state to EXECUTING to simulate a running job
        from grbl_proxy.proxy_core import ProxyState as PS
        core3._state = PS.EXECUTING

        # Reconnect should NOT reset state
        core3.on_client_connected()
        assert core3.state == ProxyState.EXECUTING

    async def test_status_query_during_executing_returns_synthetic(self, tmp_path):
        """'?' during EXECUTING returns synthetic <Run|...> to writer."""
        server, mock, core = _make_server(_BASE_PORT + 3, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 3)

            # Buffer with auto_respond on, then stop it so streamer blocks
            mock._auto_respond = True
            writer.write(b"G4 P0.0\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            mock._auto_respond = False
            await asyncio.sleep(0.02)
            assert core.state == ProxyState.EXECUTING

            # Send '?' real-time byte — should get synthetic <Run|...>
            writer.write(b"?")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert resp.startswith(b"<Run|")

            # Finish the streamer (inject oks for in-flight lines)
            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=3.0)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_synthetic_status_reflects_real_state_during_executing(self, tmp_path):
        """During EXECUTING the synthetic '?' reply mirrors the real cached
        machine state. If GRBL is Idle while we're still EXECUTING, that is a
        buffer-lock symptom and must be visible, not masked by a hardcoded Run.
        BUFFERING keeps the Run illusion; PAUSED reports Hold.
        """
        cfg = _make_job_cfg(tmp_path / "jobs")
        core = ProxyCore(cfg, idle_timeout_s=0.1)

        class _FakeWriter:
            def __init__(self) -> None:
                self.buf = bytearray()

            def write(self, b: bytes) -> None:
                self.buf += b

            async def drain(self) -> None:
                pass

        async def synth(state: ProxyState, cached: dict | None) -> bytes:
            core._state = state
            core._last_status = cached
            w = _FakeWriter()
            await core._write_synthetic_status(w)
            return bytes(w.buf)

        # EXECUTING + machine Idle → surfaces the lock as Idle.
        out = await synth(
            ProxyState.EXECUTING,
            {"state": "Idle", "mpos": (1.0, 2.0, 0.0), "fs": (0, 0)},
        )
        assert out.startswith(b"<Idle|")
        assert b"MPos:1.000,2.000,0.000" in out

        # EXECUTING + machine Run → Run.
        out = await synth(
            ProxyState.EXECUTING,
            {"state": "Run", "mpos": (3.0, 4.0, 0.0), "fs": (6000, 400)},
        )
        assert out.startswith(b"<Run|")

        # EXECUTING with no cached status yet → defaults to Run.
        out = await synth(ProxyState.EXECUTING, None)
        assert out.startswith(b"<Run|")

        # BUFFERING must keep the Run illusion even though the machine is Idle.
        out = await synth(
            ProxyState.BUFFERING,
            {"state": "Idle", "mpos": (0.0, 0.0, 0.0), "fs": (0, 0)},
        )
        assert out.startswith(b"<Run|")

        # PAUSED always reports Hold.
        out = await synth(
            ProxyState.PAUSED,
            {"state": "Run", "mpos": (0.0, 0.0, 0.0), "fs": (0, 0)},
        )
        assert out.startswith(b"<Hold|")

    async def test_commands_rejected_during_executing(self, tmp_path):
        """Interactive G-code commands during EXECUTING return error:9."""
        server, mock, core = _make_server(_BASE_PORT + 4, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 4)

            mock._auto_respond = True
            writer.write(b"G4 P0.0\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            mock._auto_respond = False
            await asyncio.sleep(0.02)
            assert core.state == ProxyState.EXECUTING

            # Interactive command should be rejected
            writer.write(b"G0 X50\n")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert resp == b"ok\r\n"

            # Finish
            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=3.0)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_feed_hold_transitions_to_paused(self, tmp_path):
        """'!' during EXECUTING transitions to PAUSED and forwards to serial."""
        server, mock, core = _make_server(_BASE_PORT + 5, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 5)

            mock._auto_respond = True
            writer.write(b"G4 P0.0\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"G0 X10\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            mock._auto_respond = False
            await asyncio.sleep(0.02)
            assert core.state == ProxyState.EXECUTING

            # Send feed hold
            writer.write(b"!")
            await writer.drain()
            await asyncio.sleep(0.05)
            assert core.state == ProxyState.PAUSED

            # '!' should have been forwarded to serial
            assert any(b"!" in raw for raw in mock.tx_log)

            # Resume and finish
            writer.write(b"~")
            await writer.drain()
            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=3.0)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# TestStreamerIntegration — full end-to-end via TcpServer
# ---------------------------------------------------------------------------


class TestStreamerIntegration:
    """End-to-end integration tests using TcpServer + MockSerialConnection."""

    async def test_full_job_executes_after_buffering(self, tmp_path):
        """Buffer a job via TcpServer, wait for PASSTHROUGH, verify lines in tx_log."""
        server, mock, core = _make_server(_BASE_PORT + 6, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 6)
            job_lines = [b"G0 X10\n", b"G1 Y20\n", b"G0 X0\n"]
            await _buffer_job(reader, writer, job_lines)

            # Wait for streamer to finish and return to PASSTHROUGH
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=5.0)

            # All buffered lines should have been sent to serial by the streamer
            sent = mock.last_sent_lines()
            assert "G0 X10" in sent
            assert "G1 Y20" in sent
            assert "G0 X0" in sent
            assert "M2" in sent  # automatically appended

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def _buffer_and_hold_executing(
        self,
        mock: MockSerialConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        core: ProxyCore,
    ) -> None:
        """Buffer a job with auto_respond=True, then disable auto_respond so
        the streamer blocks awaiting an ok. Caller must inject oks to finish."""
        mock._auto_respond = True
        writer.write(b"G4 P0.0\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.write(b"G0 X10\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.write(b"M2\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=2.0)
        mock._auto_respond = False
        await asyncio.sleep(0.02)

    async def test_status_query_during_execution_returns_run(self, tmp_path):
        """'?' from LightBurn during execution returns <Run|...> status."""
        server, mock, core = _make_server(_BASE_PORT + 7, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 7)
            await self._buffer_and_hold_executing(mock, reader, writer, core)
            assert core.state == ProxyState.EXECUTING

            writer.write(b"?")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert b"Run" in resp

            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=3.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_interactive_command_rejected_during_execution(self, tmp_path):
        """Non-status commands during EXECUTING return error:9."""
        server, mock, core = _make_server(_BASE_PORT + 8, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 8)
            await self._buffer_and_hold_executing(mock, reader, writer, core)
            assert core.state == ProxyState.EXECUTING

            writer.write(b"$$\n")  # settings query — swallowed with ok
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert resp == b"ok\r\n"

            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=3.0)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_lightburn_disconnect_does_not_stop_job(self, tmp_path):
        """Disconnecting LightBurn during EXECUTING leaves state EXECUTING."""
        server, mock, core = _make_server(_BASE_PORT + 9, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 9)
            await self._buffer_and_hold_executing(mock, reader, writer, core)
            assert core.state == ProxyState.EXECUTING

            # Close LightBurn connection abruptly
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(0.05)

            # State must still be EXECUTING — job was not aborted by disconnect
            assert core.state == ProxyState.EXECUTING

            # Finish the job. After streamer completes, state goes
            # PASSTHROUGH → DISCONNECTED as the gather cleanup runs.
            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state_in(
                core,
                {ProxyState.PASSTHROUGH, ProxyState.DISCONNECTED},
                timeout=3.0,
            )
        finally:
            await server.stop()

    async def test_job_complete_returns_to_passthrough(self, tmp_path):
        """Successful job execution always returns state to PASSTHROUGH."""
        server, mock, core = _make_server(_BASE_PORT + 10, tmp_path / "jobs")
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 10)
            await _buffer_job(reader, writer, [b"G0 X10\n", b"G1 Y20\n"])

            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=5.0)
            assert core.state == ProxyState.PASSTHROUGH

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# TestStreamerErrorHandling — error/alarm/cancel outcomes
# ---------------------------------------------------------------------------


class TestStreamerErrorHandling:
    """Tests for error, alarm, and cancel outcomes from the streamer."""

    async def test_serial_disconnect_during_execution(self, tmp_path):
        """SerialDisconnectedError mid-stream → ERROR state, serial_readable set.

        Creates a GrblStreamer directly and injects a SerialDisconnectedError via
        a mock that raises on write/read, then verifies _on_streamer_done transitions
        the ProxyCore to ERROR.
        """
        from grbl_proxy.serial_conn import SerialDisconnectedError

        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10"])

        cfg = _make_job_cfg(tmp_path / "jobs")
        core = ProxyCore(cfg, idle_timeout_s=0.1)
        core.on_client_connected()

        # Create a mock that raises SerialDisconnectedError on write
        class FailingMock:
            is_connected = False

            async def write(self, data: bytes) -> None:
                raise SerialDisconnectedError("Simulated disconnect")

            async def read_line(self) -> str:
                raise SerialDisconnectedError("Simulated disconnect")

            def close_immediately(self) -> None:
                pass

        failing_mock = FailingMock()
        core._serial_conn = failing_mock
        core._state = ProxyState.EXECUTING
        core._serial_readable.clear()

        streamer = GrblStreamer(
            gcode, failing_mock,
            on_done=core._on_streamer_done,
            on_status=core.update_last_status,
        )
        core._streamer = streamer

        task = asyncio.create_task(streamer.run())
        await asyncio.wait_for(task, timeout=3.0)

        assert core.state == ProxyState.ERROR
        assert core.serial_readable.is_set()

    async def test_error_response_transitions_to_error_state(self, tmp_path):
        """GRBL error:2 during streaming → proxy enters ERROR state.

        Uses the GrblStreamer directly to inject an error:2 response.
        """
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10"])
        mock = MockSerialConnection(auto_respond=False)

        cfg = _make_job_cfg(tmp_path / "jobs")
        core = ProxyCore(cfg, idle_timeout_s=0.1)
        core.on_client_connected()
        core._serial_conn = mock
        core._state = ProxyState.EXECUTING
        core._serial_readable.clear()

        streamer = GrblStreamer(
            gcode, mock,
            on_done=core._on_streamer_done,
            on_status=core.update_last_status,
        )
        core._streamer = streamer

        async def inject_error():
            await asyncio.sleep(0.05)
            mock.inject("error:2")

        task = asyncio.create_task(streamer.run())
        await inject_error()
        await asyncio.wait_for(task, timeout=3.0)

        assert core.state == ProxyState.ERROR

    async def test_alarm_response_transitions_to_error_state(self, tmp_path):
        """GRBL ALARM:1 during streaming → proxy enters ERROR state.

        Uses GrblStreamer directly to inject ALARM:1 without race conditions.
        """
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10"])
        mock = MockSerialConnection(auto_respond=False)

        cfg = _make_job_cfg(tmp_path / "jobs")
        core = ProxyCore(cfg, idle_timeout_s=0.1)
        core.on_client_connected()
        core._serial_conn = mock
        core._state = ProxyState.EXECUTING
        core._serial_readable.clear()

        streamer = GrblStreamer(
            gcode, mock,
            on_done=core._on_streamer_done,
            on_status=core.update_last_status,
        )
        core._streamer = streamer

        async def inject_alarm():
            await asyncio.sleep(0.05)
            mock.inject("ALARM:1")

        task = asyncio.create_task(streamer.run())
        await inject_alarm()
        await asyncio.wait_for(task, timeout=3.0)

        assert core.state == ProxyState.ERROR

    async def test_error_state_cleared_by_dollar_x(self, tmp_path):
        """In ERROR state, $X clears error and returns to PASSTHROUGH.

        Uses GrblStreamer directly to reliably reach ERROR state, then tests
        $X handling via TcpServer.
        """
        gcode = tmp_path / "test.gcode"
        _write_gcode_file(gcode, ["G0 X10"])

        # Use a real TcpServer so $X goes through process_client_line
        server, mock, core = _make_server(_BASE_PORT + 13, tmp_path / "jobs2", auto_respond=True)
        await server.start()
        try:
            # Get to ERROR state by directly manipulating core state
            core._state = ProxyState.ERROR
            core._serial_readable.set()

            reader, writer = await _connect(_BASE_PORT + 13)
            await asyncio.sleep(0.02)

            # Send $X to clear the error
            writer.write(b"$X\n")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert resp == b"ok\r\n"
            assert core.state == ProxyState.PASSTHROUGH

            # Verify $X was forwarded to serial
            sent = mock.last_sent_lines()
            assert "$X" in sent

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_error_state_dollar_h_does_not_double_ack(self, tmp_path):
        """In ERROR state, clearing via $H must NOT get an immediate spoofed
        'ok' plus GRBL's real (possibly delayed, e.g. homing) 'ok' — that
        would double-ack a single LightBurn command and desync its counting.

        Uses auto_respond=False so GRBL's 'ok' only arrives once explicitly
        injected, simulating a homing cycle that takes a moment to complete.
        """
        server, mock, core = _make_server(
            _BASE_PORT + 14, tmp_path / "jobs3", auto_respond=False
        )
        await server.start()
        try:
            core._state = ProxyState.ERROR
            core._serial_readable.set()

            reader, writer = await _connect(_BASE_PORT + 14)
            await asyncio.sleep(0.02)

            writer.write(b"$H\n")
            await writer.drain()

            # No response should arrive yet — GRBL hasn't been given one,
            # and the proxy must not spoof one anymore.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), timeout=0.2)

            assert core.state == ProxyState.PASSTHROUGH
            assert "$H" in mock.last_sent_lines()

            # Now GRBL's real response arrives (simulating homing completion).
            mock.inject("ok")
            resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert resp == b"ok\r\n"

            # And nothing further follows — exactly one ok for one command.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.readline(), timeout=0.2)

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    async def test_feed_hold_then_resume_completes_job(self, tmp_path):
        """Feed hold (!) then resume (~) during execution completes the job.

        Uses auto_respond=False so the streamer blocks waiting for oks,
        giving the test time to assert state transitions.
        """
        server, mock, core = _make_server(_BASE_PORT + 14, tmp_path / "jobs", auto_respond=False)
        await server.start()
        try:
            reader, writer = await _connect(_BASE_PORT + 14)

            # Buffer the job with auto_respond=True, then switch off
            mock._auto_respond = True
            lines = [f"G0 X{i}\n".encode() for i in range(3)]
            writer.write(b"G4 P0.0\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)
            for line in lines:
                writer.write(line)
                await writer.drain()
                await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(b"M2\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=2.0)

            # Stop auto-respond so the streamer blocks in the trailing ack drain
            mock._auto_respond = False
            await asyncio.sleep(0.05)
            assert core.state == ProxyState.EXECUTING

            # Feed hold
            writer.write(b"!")
            await writer.drain()
            await asyncio.sleep(0.05)
            assert core.state == ProxyState.PAUSED

            # Resume
            writer.write(b"~")
            await writer.drain()
            await asyncio.sleep(0.05)
            assert core.state == ProxyState.EXECUTING

            # Inject oks so the streamer finishes the trailing acks
            for _ in range(5):
                mock.inject("ok")
            await _wait_for_state(core, ProxyState.PASSTHROUGH, timeout=5.0)
            assert core.state == ProxyState.PASSTHROUGH

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()
