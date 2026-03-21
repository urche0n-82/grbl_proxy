"""Ring buffer for recent serial console I/O.

Populated by a logging.Handler attached to grbl_proxy.tcp_server at DEBUG
level. The handler pattern-matches log messages that tcp_server emits for
each serial line sent and received.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque

# tcp_server logs serial RX at DEBUG as:  Serial→TCP: b'ok\n'
# and TX (routed lines) as:               Route [Passthrough]: G0 X10
_RX_RE = re.compile(r"Serial[→>-]+TCP:\s*(.*)", re.IGNORECASE)
_TX_RE = re.compile(r"Route\s*\[[^\]]+\]:\s*(.*)")


class ConsoleLog:
    """Thread-safe ring buffer of recent serial console lines."""

    def __init__(self, maxlen: int = 200) -> None:
        self._lines: deque[dict] = deque(maxlen=maxlen)

    def add(self, direction: str, text: str) -> None:
        """Append a line. direction is 'rx' (GRBL→proxy) or 'tx' (proxy→GRBL)."""
        self._lines.append({"t": time.time(), "dir": direction, "text": text.strip()})

    def recent(self, n: int = 50) -> list[dict]:
        """Return up to n most recent entries (oldest first)."""
        items = list(self._lines)
        return items[-n:] if len(items) > n else items


class _ConsoleLogHandler(logging.Handler):
    """Logging handler that feeds ConsoleLog from tcp_server DEBUG output."""

    def __init__(self, console: ConsoleLog) -> None:
        super().__init__(level=logging.DEBUG)
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            m = _RX_RE.search(msg)
            if m:
                # Value is repr of bytes, e.g. b'ok\n' — decode it for display
                raw = m.group(1).strip()
                try:
                    text = eval(raw).decode(errors="replace").strip()  # noqa: S307
                except Exception:
                    text = raw
                self._console.add("rx", text)
                return
            m = _TX_RE.search(msg)
            if m:
                self._console.add("tx", m.group(1).strip())
        except Exception:
            self.handleError(record)
