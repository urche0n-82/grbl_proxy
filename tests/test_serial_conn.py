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
from grbl_proxy.serial_conn import SerialConnection


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
