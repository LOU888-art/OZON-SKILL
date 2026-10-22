"""Append-only operation audit log without storing request bodies or secrets."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ActivityRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def append(
        self,
        *,
        actor: str,
        method: str,
        path: str,
        status: int,
        remote_address: str = "",
    ) -> dict[str, Any]:
        record = {
            "activity_id": uuid.uuid4().hex,
            "created_at": _now(),
            "actor": str(actor or "unknown")[:64],
            "method": str(method or "")[:12],
            "path": str(path or "")[:500],
            "status": int(status),
            "outcome": "SUCCESS" if 200 <= int(status) < 400 else "FAILED",
            "remote_address": str(remote_address or "")[:80],
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
        return record

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        result: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result
