"""Tests for SerialConnection's read serialization (A2 fix).

These exercise the real SerialConnection.read_line() — not the mock — by
injecting a fake pyserial object whose blocking readline() we control from the
test. They verify that two readline() worker threads never touch the fd at
once, and that cancelling a read waits for its worker thread before releasing
the read lock (so no orphaned readline overlaps the next reader, and the port
is not closed on a client disconnect).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from grbl_proxy.config import SerialConfig
import serial

from grbl_proxy.serial_conn import SerialConnection, SerialDisconnectedError


class _BlockingSerial:
    """Fake pyserial Serial whose read() blocks until released, tracking how
    many read() calls are simultaneously in flight."""

    def __init__(self, payload: bytes = b"ok\r\n") -> None:
        self._lock = threading.Lock()
        self.active = 0          # read() calls currently running
        self.max_active = 0      # high-water mark of concurrent calls
        self.calls = 0           # total read() calls started
        self.release = threading.Event()
        self._payload = payload

    in_waiting = 0  # forces read(max(1, in_waiting)) -> read(1)-style blocking

    def read(self, _n: int = 1) -> bytes:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.release.wait(timeout=2.0)
            return self._payload
        finally:
            with self._lock:
                self.active -= 1

    def close(self) -> None:
        pass


class _ScriptedSerial:
    """Fake serial that hands back a scripted sequence of raw chunks, letting a
    test split a line across reads (b"o", b"k\\r\\n") the way a timing-unlucky
    read would."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    in_waiting = 0

    def read(self, _n: int = 1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        pass


def _make_conn(fake) -> SerialConnection:
    conn = SerialConnection(SerialConfig(port="/dev/null"), port="/dev/null")
    conn._serial = fake  # inject fake device
    conn._connected.set()
    return conn


class TestPortRecovery:
    """A USB controller that re-enumerates gets a NEW node (ttyACM1) while the
    old one can linger until every fd on it is closed. The proxy has to follow
    it, and must not be the thing pinning the old node."""

    def _auto_conn(self) -> SerialConnection:
        return SerialConnection(SerialConfig(port="auto"), port="/dev/ttyACM0")

    def test_rescan_follows_device_to_a_new_node(self, monkeypatch):
        conn = self._auto_conn()
        # Controller came back as ttyACM1; ttyACM0 lingers as a dead node.
        monkeypatch.setattr(
            "grbl_proxy.serial_conn.list_serial_candidates",
            lambda: ["/dev/ttyACM1", "/dev/ttyACM0"],  # newest first
        )
        conn._rescan_port()
        assert conn.port == "/dev/ttyACM1"

    def test_rescan_resets_the_settle_timer_for_the_new_node(self, monkeypatch):
        conn = self._auto_conn()
        conn._port_first_seen = 123.0  # settling on the OLD node
        monkeypatch.setattr(
            "grbl_proxy.serial_conn.list_serial_candidates",
            lambda: ["/dev/ttyACM1"],
        )
        conn._rescan_port()
        assert conn._port_first_seen is None

    def test_rescan_keeps_current_path_when_nothing_present(self, monkeypatch):
        """No candidates yet (laser powered off) — keep waiting on the current
        path rather than clearing it or inventing a fallback."""
        conn = self._auto_conn()
        monkeypatch.setattr(
            "grbl_proxy.serial_conn.list_serial_candidates", lambda: []
        )
        conn._rescan_port()
        assert conn.port == "/dev/ttyACM0"

    def test_explicit_port_is_never_rescanned(self, monkeypatch):
        """An operator-pinned path (or a udev symlink) must be honoured."""
        conn = SerialConnection(
            SerialConfig(port="/dev/grbl-laser"), port="/dev/grbl-laser"
        )
        monkeypatch.setattr(
            "grbl_proxy.serial_conn.list_serial_candidates",
            lambda: ["/dev/ttyACM1"],
        )
        conn._rescan_port()
        assert conn.port == "/dev/grbl-laser"

    async def test_write_error_releases_the_fd(self):
        """A failed write must CLOSE the port, not merely mark it disconnected.
        Leaving the fd open pins the kernel node, forcing the controller to
        re-enumerate as ttyACM1 — the actual cause of the renumbering."""
        class _EIOSerial:
            def write(self, data):
                raise serial.SerialException("write failed: [Errno 5] I/O error")
            def close(self):
                pass

        conn = self._auto_conn()
        conn._serial = _EIOSerial()
        conn._connected.set()

        with pytest.raises(SerialDisconnectedError):
            await conn.write(b"?")

        assert conn._serial is None      # fd released...
        assert not conn.is_connected     # ...and marked disconnected

    async def test_stale_fd_after_io_error_is_swept_up(self, monkeypatch):
        """Defence in depth: if some path leaves the fd open while marked
        disconnected, the reconnect loop releases it on its next tick."""
        conn = self._auto_conn()
        conn._serial = _ScriptedSerial([])
        conn._connected.clear()          # marked down, but fd still open (the leak)
        # Node still present, so it's the "stale fd" branch, not "vanished".
        monkeypatch.setattr("grbl_proxy.serial_conn.os.path.exists", lambda p: True)
        monkeypatch.setattr(
            "grbl_proxy.serial_conn.list_serial_candidates", lambda: ["/dev/ttyACM0"]
        )

        task = asyncio.create_task(conn.run_reconnect_loop())
        await asyncio.sleep(1.3)
        conn.signal_shutdown()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert conn._serial is None      # leaked fd swept up

    async def test_vanished_node_closes_the_port_promptly(self, monkeypatch):
        """The fd must be dropped when the node disappears — holding it is what
        forces the controller to re-enumerate under a new index."""
        conn = self._auto_conn()
        conn._serial = _ScriptedSerial([])   # pretend open
        conn._connected.set()
        monkeypatch.setattr("grbl_proxy.serial_conn.os.path.exists", lambda p: False)
        monkeypatch.setattr(
            "grbl_proxy.serial_conn.list_serial_candidates", lambda: []
        )

        task = asyncio.create_task(conn.run_reconnect_loop())
        await asyncio.sleep(1.3)  # one monitor tick
        conn.signal_shutdown()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert not conn.is_connected
        assert conn._serial is None  # fd released, node free to be reclaimed


class TestLineFraming:
    """read_line() must only ever emit complete, newline-terminated lines. A
    read landing mid-line has to buffer the fragment, not hand it back as a
    line — that would split one message into two garbage ones and destroy any
    ok it carried (permanent flow-control drift)."""

    async def test_line_split_across_reads_is_reassembled(self):
        # "ok\r\n" arriving as three separate reads must yield exactly one line.
        conn = _make_conn(_ScriptedSerial([b"o", b"k", b"\r\n"]))
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == "ok"

    async def test_partial_tail_is_not_emitted_on_timeout(self):
        # Data, then a timeout (b"") mid-line: the fragment must stay buffered
        # and read_line must report "no data" rather than inventing a line.
        conn = _make_conn(_ScriptedSerial([b"ok\r\nGrb", b"", b"l 1.1f\r\n"]))
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == "ok"
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == ""  # idle tick
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == "Grbl 1.1f"

    async def test_multiple_lines_in_one_chunk_are_split(self):
        conn = _make_conn(_ScriptedSerial([b"ok\r\nok\r\n<Idle|FS:0,0>\r\n"]))
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == "ok"
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == "ok"
        assert await asyncio.wait_for(conn.read_line(), timeout=2.0) == "<Idle|FS:0,0>"

    async def test_close_discards_partial_line(self):
        conn = _make_conn(_ScriptedSerial([b"partial-frag"]))
        await asyncio.wait_for(conn.read_line(), timeout=2.0)  # buffers the tail
        assert conn._rx_buf  # fragment retained
        conn.close_immediately()
        assert not conn._rx_buf  # and dropped on close, not carried over


async def test_concurrent_reads_are_serialized():
    """Two read_line() calls must not run two readline() threads at once."""
    fake = _BlockingSerial()
    conn = _make_conn(fake)

    t1 = asyncio.create_task(conn.read_line())
    t2 = asyncio.create_task(conn.read_line())
    await asyncio.sleep(0.15)  # let both tasks get as far as they can

    # Only the first read's thread should be running; the second waits on the lock.
    assert fake.active == 1
    assert fake.calls == 1
    assert fake.max_active == 1

    fake.release.set()
    r1 = await asyncio.wait_for(t1, timeout=2.0)
    r2 = await asyncio.wait_for(t2, timeout=2.0)

    assert r1 == "ok"
    assert r2 == "ok"
    assert fake.calls == 2
    assert fake.max_active == 1  # never overlapped


async def test_cancel_waits_for_worker_thread():
    """Cancelling read_line() must not release the lock until the worker thread
    finishes, and must NOT close the port."""
    fake = _BlockingSerial()
    conn = _make_conn(fake)

    t = asyncio.create_task(conn.read_line())
    await asyncio.sleep(0.15)
    assert fake.active == 1  # worker thread is blocked in readline()

    t.cancel()
    await asyncio.sleep(0.15)
    # The task must still be pending: it's waiting for the worker thread to
    # finish before propagating the cancellation.
    assert not t.done()
    assert fake.active == 1

    # Port must remain open across the cancellation (A2: no teardown on disconnect).
    assert conn.is_connected

    fake.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t, timeout=2.0)

    await asyncio.sleep(0.05)
    assert fake.active == 0  # worker thread finished
    assert conn.is_connected  # still not closed


async def test_next_read_waits_for_cancelled_reads_thread():
    """After a read is cancelled (thread still draining), the next reader must
    wait for that thread rather than starting a second concurrent readline()."""
    fake = _BlockingSerial()
    conn = _make_conn(fake)

    t1 = asyncio.create_task(conn.read_line())
    await asyncio.sleep(0.15)
    assert fake.active == 1

    t1.cancel()  # thread keeps running; read_line holds the lock until it ends
    await asyncio.sleep(0.05)

    t2 = asyncio.create_task(conn.read_line())
    await asyncio.sleep(0.15)
    # t2 must be blocked on the read lock — still only one thread in readline().
    assert fake.max_active == 1
    assert fake.active == 1

    fake.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t1, timeout=2.0)
    r2 = await asyncio.wait_for(t2, timeout=2.0)
    assert r2 == "ok"
    assert fake.max_active == 1
