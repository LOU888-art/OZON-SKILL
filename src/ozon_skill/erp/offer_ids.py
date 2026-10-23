"""Persistent QSM offer-id allocation for Ozon listings."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence


_OFFER_ID = re.compile(r"^QSM(\d{4})(\d{2,})$")
_BUSINESS_TIMEZONE = timezone(timedelta(hours=8))


class OfferIdRegistryError(RuntimeError):
    """A safe, user-facing offer-id allocation failure."""


class OfferIdRegistry:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path.resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": "1.0", "daily_next": {}, "assignments": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OfferIdRegistryError("Ozon 商品货号序号表无法读取") from exc
        if not isinstance(value, dict):
            raise OfferIdRegistryError("Ozon 商品货号序号表格式无效")
        value.setdefault("daily_next", {})
        value.setdefault("assignments", {})
        if not isinstance(value["daily_next"], dict) or not isinstance(
            value["assignments"], dict
        ):
            raise OfferIdRegistryError("Ozon 商品货号序号表格式无效")
        return value

    def _write(self, value: dict) -> None:
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

    def assign(
        self,
        keys: Sequence[str],
        *,
        reserved_ids: Sequence[str] = (),
    ) -> list[str]:
        normalized = [str(key or "").strip() for key in keys]
        if not normalized or any(not key for key in normalized):
            raise OfferIdRegistryError("商品货号分配键不能为空")
        if len(set(normalized)) != len(normalized):
            raise OfferIdRegistryError("商品货号分配键重复")
        with self._lock:
            value = self._read()
            assignments = value["assignments"]
            day = self._clock().astimezone(_BUSINESS_TIMEZONE).strftime("%m%d")
            reserved = {
                str(item or "").strip() for item in reserved_ids if str(item or "").strip()
            }
            used_today = [
                int(match.group(2))
                for item in (*assignments.values(), *reserved)
                if (match := _OFFER_ID.fullmatch(str(item))) and match.group(1) == day
            ]
            next_sequence = max(
                int(value["daily_next"].get(day) or 1),
                max(used_today, default=0) + 1,
            )
            changed = False
            result: list[str] = []
            for key in normalized:
                existing = str(assignments.get(key) or "")
                if _OFFER_ID.fullmatch(existing):
                    result.append(existing)
                    continue
                offer_id = f"QSM{day}{next_sequence:02d}"
                while offer_id in reserved:
                    next_sequence += 1
                    offer_id = f"QSM{day}{next_sequence:02d}"
                assignments[key] = offer_id
                result.append(offer_id)
                next_sequence += 1
                changed = True
            value["daily_next"][day] = next_sequence
            if changed:
                value["updated_at"] = self._clock().astimezone(
                    _BUSINESS_TIMEZONE
                ).isoformat()
                self._write(value)
            return result
