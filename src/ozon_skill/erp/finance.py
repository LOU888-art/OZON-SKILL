"""Store-specific profit and replenishment settings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping


DEFAULT_FINANCE_SETTINGS = {
    "cny_to_rub_rate": 12.6,
    "commission_percent": 10.0,
    "return_loss_percent": 3.0,
    "withdrawal_percent": 1.2,
    "advertising_percent": 5.0,
    "last_mile_percent": 2.0,
    "target_margin_percent": 30.0,
    "domestic_misc_cny": 0.0,
    "logistics_mode": "OGL",
    "postal_transport": "空运",
    "lead_time_days": 15,
    "safety_stock_days": 7,
}


class FinanceSettingsError(RuntimeError):
    """A user-facing finance configuration failure."""


class FinanceSettingsRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "stores": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinanceSettingsError("利润设置文件无法读取") from exc
        if not isinstance(value, dict) or not isinstance(value.get("stores"), dict):
            raise FinanceSettingsError("利润设置文件格式无效")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
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

    def get(self, store_id: str) -> dict[str, Any]:
        with self._lock:
            saved = self._read()["stores"].get(store_id) or {}
        result = {**DEFAULT_FINANCE_SETTINGS, **saved}
        result["store_id"] = store_id
        result["configured"] = bool(saved)
        return result

    def save(self, store_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        bounds = {
            "cny_to_rub_rate": (0.01, 1000.0),
            "commission_percent": (0.0, 100.0),
            "return_loss_percent": (0.0, 100.0),
            "withdrawal_percent": (0.0, 100.0),
            "advertising_percent": (0.0, 100.0),
            "last_mile_percent": (0.0, 100.0),
            "target_margin_percent": (0.0, 100.0),
            "domestic_misc_cny": (0.0, 1000000.0),
            "lead_time_days": (1, 365),
            "safety_stock_days": (0, 365),
        }
        normalized: dict[str, Any] = {}
        for key, (minimum, maximum) in bounds.items():
            try:
                number = float(value.get(key))
            except (TypeError, ValueError) as exc:
                raise FinanceSettingsError(f"{key} 必须是数字") from exc
            if number < minimum or number > maximum:
                raise FinanceSettingsError(f"{key} 必须在 {minimum}–{maximum} 之间")
            normalized[key] = int(number) if key.endswith("_days") else round(number, 4)
        logistics_mode = str(value.get("logistics_mode") or "").strip()
        postal_transport = str(value.get("postal_transport") or "").strip()
        if logistics_mode not in {"OGL", "邮政"}:
            raise FinanceSettingsError("物流方式必须是 OGL 或邮政")
        if postal_transport not in {"空运", "陆运"}:
            raise FinanceSettingsError("邮政运输必须是空运或陆运")
        normalized["logistics_mode"] = logistics_mode
        normalized["postal_transport"] = postal_transport
        variable_total = sum(
            normalized[key]
            for key in (
                "commission_percent",
                "return_loss_percent",
                "withdrawal_percent",
                "advertising_percent",
                "last_mile_percent",
            )
        )
        if variable_total >= 100:
            raise FinanceSettingsError("佣金及各项比例费率合计必须小于 100%")
        if variable_total + normalized["target_margin_percent"] >= 100:
            raise FinanceSettingsError("费率与目标利润率合计必须小于 100%")
        with self._lock:
            document = self._read()
            document["stores"][store_id] = normalized
            self._write(document)
        return self.get(store_id)
