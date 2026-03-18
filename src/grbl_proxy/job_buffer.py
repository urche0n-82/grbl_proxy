"""File-backed G-code job buffer for grbl-proxy.

Receives G-code lines from the proxy during Buffering state and writes them
to disk so arbitrarily large files work without exhausting RAM. A metadata
JSON file is written alongside on finalization.

There is only ever one writer (the _tcp_to_serial relay coroutine), so no
locking is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_FLUSH_BATCH = 50  # lines between disk flushes


@dataclass
class JobMetadata:
    path: Path
    line_count: int
    start_time: float
    end_time: float
    duration_s: float


class JobBuffer:
    """Write G-code lines to disk one at a time, flushing in batches.

    Lifecycle:
        buf = JobBuffer(storage_dir)
        await buf.open()
        await buf.write_line(line)   # repeated
        meta = await buf.finalize()  # or await buf.discard()
    """

    def __init__(self, storage_dir: Path, start_time: float | None = None) -> None:
        self._storage_dir = storage_dir
        self._start_time = start_time if start_time is not None else time.time()
        self._line_count = 0
        self._pending: list[str] = []
        self._file = None
        self._path: Path | None = None
        self._finalized = False
        self._discarded = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Create storage directory and open current.gcode for writing."""
        await asyncio.to_thread(self._open_sync)

    async def write_line(self, line: str) -> None:
        """Append one line to the buffer. Flushes to disk every _FLUSH_BATCH lines."""
        self._pending.append(line + "\n")
        self._line_count += 1
        if len(self._pending) >= _FLUSH_BATCH:
            await self._flush()

    async def discard(self) -> None:
        """Close and delete the job file and metadata file (if they exist)."""
        self._discarded = True
        await asyncio.to_thread(self._discard_sync)
        logger.debug("Job buffer discarded")

    async def finalize(self) -> JobMetadata:
        """Flush, close, write metadata JSON, and return JobMetadata."""
        await self._flush()
        end_time = time.time()
        meta = JobMetadata(
            path=self._path,
            line_count=self._line_count,
            start_time=self._start_time,
            end_time=end_time,
            duration_s=end_time - self._start_time,
        )
        await asyncio.to_thread(self._finalize_sync, meta)
        self._finalized = True
        logger.debug(
            "Job buffer finalized: %d lines at %s", self._line_count, self._path
        )
        return meta

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def line_count(self) -> int:
        return self._line_count

    @property
    def is_open(self) -> bool:
        return self._file is not None

    @property
    def path(self) -> Path | None:
        return self._path

    # ------------------------------------------------------------------
    # Internal (run in threads via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _open_sync(self) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._storage_dir / "current.gcode"
        self._file = open(self._path, "w", encoding="utf-8")
        logger.info("Job buffer opened: %s", self._path)

    def _flush_sync(self, data: str) -> None:
        if self._file is not None:
            self._file.write(data)
            self._file.flush()

    def _discard_sync(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        for name in ("current.gcode", "current.meta.json"):
            p = self._storage_dir / name
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    def _finalize_sync(self, meta: JobMetadata) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        # Write metadata as JSON (convert Path to str for serialisation)
        meta_path = self._storage_dir / "current.meta.json"
        data = asdict(meta)
        data["path"] = str(meta.path)
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def _flush(self) -> None:
        if not self._pending:
            return
        data = "".join(self._pending)
        self._pending.clear()
        await asyncio.to_thread(self._flush_sync, data)
