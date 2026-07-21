"""Pure functions for parsing and classifying GRBL 1.1 protocol messages.

No I/O, no state, no asyncio. Designed for use by both the Phase 1 passthrough
relay (snooping) and Phase 2+ proxy_core state machine.
"""

from __future__ import annotations

import re
from typing import TypedDict


# Real-time command bytes — bypass the line buffer, no 'ok' response.
# Basic set: status query, feed hold, cycle resume, soft reset.
_REALTIME_BASIC = frozenset(b"?!~\x18")
# Extended real-time commands (GRBL 1.1 §4.4): all single bytes >= 0x80.
# Includes 0x85 (jog cancel), 0x84 (safety door), 0x90-0x9F (feed/spindle
# overrides), 0xA0-0xA1 (coolant toggles). Any byte in this range must be
# forwarded immediately as-is — never accumulated in a line buffer.
_REALTIME_EXTENDED = frozenset(range(0x80, 0xA2))
REALTIME_COMMANDS = _REALTIME_BASIC | _REALTIME_EXTENDED

# GRBL terminates every response line (ok, error:N, status reports, welcome
# banner) with CR+LF. A real GRBL-over-TCP device that LightBurn talks to
# directly therefore always sends "\r\n". The proxy must match this exactly:
# some LightBurn builds have a strict line parser that treats a bare "\n" as an
# incomplete line and never marks the command acknowledged (manifesting as a
# permanent "BUSY" state). Use this for every line the proxy sends BACK to
# LightBurn. Commands the proxy sends TO GRBL keep "\n" (GRBL accepts either).
LINE_TERMINATOR = "\r\n"

# Motion/laser G-code prefixes that indicate a job stream vs interactive use
MOTION_COMMAND_PREFIXES = ("G0", "G1", "G2", "G3", "M3", "M4", "M5", "S")

_STATUS_RE = re.compile(r"^<([^|>]+)((?:\|[^>]*)*)>$")
_FIELD_RE = re.compile(r"\|([A-Za-z]+):([^|>]+)")
_COORD_RE = re.compile(r"^(-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)$")


class StatusReport(TypedDict, total=False):
    state: str
    mpos: tuple[float, float, float]
    wpos: tuple[float, float, float]
    wco: tuple[float, float, float]
    fs: tuple[int, int]
    ov: tuple[int, int, int]
    pn: str
    a: str
    # Buffer state: (planner blocks free, serial RX bytes free). This is the
    # machine's own authoritative view of how much room it has — use it to
    # reconcile locally-accumulated flow-control counters, which can only drift.
    bf: tuple[int, int]


def is_status_report(line: str) -> bool:
    """Return True if line is a GRBL status report (<...>)."""
    s = line.strip()
    return s.startswith("<") and s.endswith(">")


# A complete status report block, e.g. "<Idle|MPos:0,0,0|FS:0,0>".
_STATUS_BLOCK_RE = re.compile(r"<[^<>]*>")
# A protocol message trailing a corrupted line. Deliberately NOT anchored with a
# leading \b: the observed corruption splices the ack straight onto a digit
# ("WCO:0.000,0ok"), where a word boundary would fail to match.
_TRAILING_MESSAGE_RE = re.compile(r"(ok|error:\d+|ALARM:\d+)\s*$")


def split_responses(line: str) -> list[str]:
    """Split one serial line into the GRBL protocol messages it contains.

    Normally a line holds exactly one message and this returns [line].

    Under load, ESP32 GRBL forks interleave the realtime status-report write
    with the main loop's ok/error write, emitting a single line that holds a
    *truncated* status plus a complete message:

        '<Run|MPos:201.275,148.225,0.000|Bf:0,65459|FS:1501,3|Ov:100,100,ok'
        '<Run|MPos:296.763,263.250,0.000|Bf:0,65475|FS:1958,3|WCO:0.000,0ok'

    Requiring an exact "ok" match silently drops that ack, and a character-
    counting sender then under-counts free buffer space for the rest of the job
    until it deadlocks. Recovering the trailing message here is what lets the
    stream survive the corruption — it is why LightBurn tolerates this firmware
    and a strict parser does not. The truncated status fragment is unrecoverable
    and is discarded.
    """
    s = line.strip()
    if not s:
        return []

    # Fast path: a clean, complete single message (the overwhelming majority).
    if is_ok(s) or is_error(s) or is_alarm(s) or is_status_report(s):
        return [s]

    # Possibly interleaved. Lift out any complete status blocks first, blanking
    # them so their contents can't be mistaken for a trailing message.
    messages: list[str] = []

    def _lift(m: "re.Match[str]") -> str:
        messages.append(m.group(0))
        return " " * len(m.group(0))

    remainder = _STATUS_BLOCK_RE.sub(_lift, s)

    trailing = _TRAILING_MESSAGE_RE.search(remainder)
    if trailing:
        messages.append(trailing.group(1))

    # Nothing salvageable (e.g. "[MSG:...]", banner) — hand back untouched.
    return messages or [s]


def parse_status_report(line: str) -> StatusReport | None:
    """Parse a GRBL 1.1 status report string into a dict.

    Returns None if the line is not a valid status report.

    Example input:  <Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>
    Example output: {"state": "Idle", "mpos": (0.0, 0.0, 0.0), "fs": (0, 0),
                     "wco": (0.0, 0.0, 0.0)}
    """
    s = line.strip()
    m = _STATUS_RE.match(s)
    if not m:
        return None

    result: StatusReport = {"state": m.group(1)}
    fields_str = m.group(2)

    for fm in _FIELD_RE.finditer(fields_str):
        key = fm.group(1).upper()
        val = fm.group(2)

        if key == "MPOS":
            coords = _parse_coords(val)
            if coords:
                result["mpos"] = coords
        elif key == "WPOS":
            coords = _parse_coords(val)
            if coords:
                result["wpos"] = coords
        elif key == "WCO":
            coords = _parse_coords(val)
            if coords:
                result["wco"] = coords
        elif key == "FS":
            parts = val.split(",")
            if len(parts) == 2:
                try:
                    result["fs"] = (int(parts[0]), int(parts[1]))
                except ValueError:
                    pass
        elif key == "OV":
            parts = val.split(",")
            if len(parts) == 3:
                try:
                    result["ov"] = (int(parts[0]), int(parts[1]), int(parts[2]))
                except ValueError:
                    pass
        elif key == "PN":
            result["pn"] = val
        elif key == "A":
            result["a"] = val
        elif key == "BF":
            # "Bf:<planner blocks free>,<serial RX bytes free>". NOTE: the field
            # is "Bf", not "Buf" — matching the wrong name silently dropped the
            # machine's own buffer report, which is the only thing that can
            # correct a drifted flow-control counter.
            parts = val.split(",")
            if len(parts) == 2:
                try:
                    result["bf"] = (int(parts[0]), int(parts[1]))
                except ValueError:
                    pass

    return result


def _parse_coords(val: str) -> tuple[float, float, float] | None:
    m = _COORD_RE.match(val)
    if not m:
        return None
    try:
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    except ValueError:
        return None


def is_ok(line: str) -> bool:
    """Return True if line is a GRBL 'ok' acknowledgement."""
    return line.strip() == "ok"


def is_error(line: str) -> bool:
    """Return True if line is a GRBL error response (error:N)."""
    return line.strip().startswith("error:")


def is_alarm(line: str) -> bool:
    """Return True if line is a GRBL alarm (ALARM:N)."""
    return line.strip().startswith("ALARM:")


def is_grbl_greeting(line: str) -> bool:
    """Return True if line is GRBL's startup banner."""
    s = line.strip()
    return s.startswith("Grbl ") or s.startswith("GrblHAL ")


def is_realtime_command(byte: int) -> bool:
    """Return True if a single byte is a GRBL real-time command."""
    return byte in REALTIME_COMMANDS


def is_motion_command(line: str) -> bool:
    """Return True if line is a motion or laser power command (vs query/config)."""
    s = line.strip().upper()
    return any(s.startswith(prefix) for prefix in MOTION_COMMAND_PREFIXES)


def get_error_code(line: str) -> int | None:
    """Extract the numeric error code from an error:N response."""
    s = line.strip()
    if s.startswith("error:"):
        try:
            return int(s[6:])
        except ValueError:
            pass
    return None


def get_alarm_code(line: str) -> int | None:
    """Extract the numeric alarm code from an ALARM:N response."""
    s = line.strip()
    if s.startswith("ALARM:"):
        try:
            return int(s[6:])
        except ValueError:
            pass
    return None


def make_status_response(
    state: str = "Idle",
    mpos: tuple[float, float, float] = (0.0, 0.0, 0.0),
    feed: int = 0,
    spindle: int = 0,
) -> str:
    """Build a synthetic GRBL status response string (CR+LF terminated)."""
    x, y, z = mpos
    return f"<{state}|MPos:{x:.3f},{y:.3f},{z:.3f}|FS:{feed},{spindle}>{LINE_TERMINATOR}"
