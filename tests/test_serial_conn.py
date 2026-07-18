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
    """Fake pyserial Serial whose readline() blocks until released, tracking
    how many readline() calls are simultaneously in flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0          # readline() calls currently running
        self.max_active = 0      # high-water mark of concurrent calls
        self.calls = 0           # total readline() calls started
        self.release = threading.Event()

    def readline(self) -> bytes:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.release.wait(timeout=2.0)
            return b"ok\r\n"
        finally:
            with self._lock:
                self.active -= 1

    def close(self) -> None:
        pass


def _make_conn(fake: _BlockingSerial) -> SerialConnection:
    conn = SerialConnection(SerialConfig(port="/dev/null"), port="/dev/null")
    conn._serial = fake  # inject fake device
    conn._connected.set()
    return conn


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
