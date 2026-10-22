"""Per-user read state for dynamically generated ERP notifications."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class NotificationRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "users": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": "1.0", "users": {}}
        if not isinstance(value, dict) or not isinstance(value.get("users"), dict):
            return {"schema_version": "1.0", "users": {}}
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _username(value: Any) -> str:
        return str(value or "unknown").strip().casefold()[:64]

    def decorate(
        self, username: Any, notifications: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read()
            user = document["users"].get(self._username(username)) or {}
            read_ids = set(user.get("read_ids") or [])
        return [
            {**dict(item), "read": str(item.get("notification_id")) in read_ids}
            for item in notifications
        ]

    def mark_read(self, username: Any, notification_ids: Iterable[Any]) -> None:
        ids = [str(value)[:80] for value in notification_ids if str(value).strip()]
        if not ids:
            return
        with self._lock:
            document = self._read()
            key = self._username(username)
            existing = document["users"].get(key) or {}
            read_ids = list(dict.fromkeys([*(existing.get("read_ids") or []), *ids]))
            document["users"][key] = {
                "read_ids": read_ids[-1000:],
                "updated_at": _now(),
            }
            self._write(document)
