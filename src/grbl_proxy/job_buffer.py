"""File-backed G-code job buffer for grbl-proxy.

Receives G-code lines from the proxy during Buffering state and writes them
to disk so arbitrarily large files work without exhausting RAM. A metadata
JSON file is written alongside on finalization.

On finalization, files are renamed from current.gcode → YYYYMMDD_HHMMSS.gcode
(and .meta.json) so a history of completed jobs is retained. History is capped
at max_history entries; oldest pairs are deleted when the limit is exceeded.

There is only ever one writer (the _tcp_to_serial relay coroutine), so no
locking is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
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
    source: str = "lightburn"          # "lightburn" or "upload"
    original_filename: str | None = None  # set for uploaded files


class JobBuffer:
    """Write G-code lines to disk one at a time, flushing in batches.

    Lifecycle:
        buf = JobBuffer(storage_dir)
        await buf.open()
        await buf.write_line(line)   # repeated
        meta = await buf.finalize()  # or await buf.discard()
    """

    def __init__(
        self,
        storage_dir: Path,
        start_time: float | None = None,
        source: str = "lightburn",
        original_filename: str | None = None,
        max_history: int = 20,
    ) -> None:
        self._storage_dir = storage_dir
        self._start_time = start_time if start_time is not None else time.time()
        self._source = source
        self._original_filename = original_filename
        self._max_history = max_history
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
        logger.info("Job buffer discarded")

    async def finalize(self) -> JobMetadata:
        """Flush, close, archive to timestamped files, and return JobMetadata."""
        await self._flush()
        end_time = time.time()
        timestamp = datetime.fromtimestamp(self._start_time).strftime("%Y%m%d_%H%M%S")
        archived_path = self._storage_dir / f"{timestamp}.gcode"
        meta = JobMetadata(
            path=archived_path,
            line_count=self._line_count,
            start_time=self._start_time,
            end_time=end_time,
            duration_s=end_time - self._start_time,
            source=self._source,
            original_filename=self._original_filename,
        )
        await asyncio.to_thread(self._finalize_sync, meta, timestamp)
        self._finalized = True
        logger.info(
            "Job buffer finalized: %d lines → %s", self._line_count, archived_path
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

    def _finalize_sync(self, meta: JobMetadata, timestamp: str) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

        # Rename current.gcode → timestamp.gcode
        current_gcode = self._storage_dir / "current.gcode"
        if current_gcode.exists():
            current_gcode.rename(meta.path)

        # Write timestamped metadata JSON
        meta_path = self._storage_dir / f"{timestamp}.meta.json"
        data = asdict(meta)
        data["path"] = str(meta.path)
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Enforce max_history: delete oldest pairs beyond the limit
        self._rotate_history()

    def _rotate_history(self) -> None:
        rotate_history(self._storage_dir, self._max_history)

    async def _flush(self) -> None:
        if not self._pending:
            return
        data = "".join(self._pending)
        self._pending.clear()
        await asyncio.to_thread(self._flush_sync, data)


def rotate_history(storage_dir: Path, max_history: int) -> None:
    """Delete oldest .gcode/.meta.json pairs if history exceeds max_history."""
    meta_files = sorted(storage_dir.glob("*.meta.json"))
    excess = len(meta_files) - max_history
    if excess <= 0:
        return
    for meta_file in meta_files[:excess]:
        stem = meta_file.stem
        gcode_file = storage_dir / f"{stem}.gcode"
        try:
            meta_file.unlink(missing_ok=True)
            gcode_file.unlink(missing_ok=True)
            logger.info("History rotation: deleted %s", stem)
        except Exception as e:
            logger.warning("History rotation error for %s: %s", stem, e)


def load_job_history(storage_dir: Path, max_history: int = 20) -> list[dict]:
    """Read all *.meta.json files from storage_dir, return sorted newest-first.

    Returns a list of dicts (JSON-serialisable). Capped at max_history entries.
    Skips files that fail to parse without raising.
    """
    results = []
    for meta_file in sorted(storage_dir.glob("*.meta.json"), reverse=True):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            results.append(data)
        except Exception as e:
            logger.warning("Skipping unreadable history file %s: %s", meta_file, e)
        if len(results) >= max_history:
            break
    return results
