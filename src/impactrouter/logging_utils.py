"""JSONL request logger (PRD 6.6).

One line per proxied request. The schema is stable and is the raw material
for bench/analyze_log.py -- do not add required fields without a default, and
do not remove fields, without updating the benchmark analysis script.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from impactrouter.models import RequestLogEntry


class JsonlRequestLogger:
    def __init__(self, log_path: str) -> None:
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def log(self, entry: RequestLogEntry) -> None:
        line = entry.model_dump_json() + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
