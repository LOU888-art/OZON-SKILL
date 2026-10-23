"""Persistent 1688 supplier snapshots and deterministic change detection."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


class SupplierMonitorError(RuntimeError):
    """Raised when supplier monitor data cannot be stored or compared."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _price(value: Any) -> str | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return format(number.normalize(), "f")


def snapshot_from_raw(raw: Mapping[str, Any]) -> dict[str, Any]:
    skus: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw.get("sku_list") or []):
        if not isinstance(item, Mapping):
            continue
        sku_id = str(item.get("sku_id") or f"default-{index}").strip()
        stock_value = item.get("stock")
        try:
            stock = int(stock_value) if stock_value is not None else None
        except (TypeError, ValueError):
            stock = None
        skus[sku_id] = {
            "purchase_price_cny": _price(item.get("purchase_price_cny")),
            "stock": max(0, stock) if stock is not None else None,
            "specification_text_zh": str(item.get("specification_text_zh") or "")[:300],
        }
    known_stocks = [item["stock"] for item in skus.values() if item["stock"] is not None]
    if known_stocks and len(known_stocks) == len(skus):
        stock_status = "OUT_OF_STOCK" if sum(known_stocks) == 0 else "IN_STOCK"
    else:
        stock_status = "IN_STOCK" if any(value > 0 for value in known_stocks) else "UNKNOWN"
    supplier = raw.get("supplier_info") or {}
    if not isinstance(supplier, Mapping):
        supplier = {}
    return {
        "title_zh": str(raw.get("title_zh") or "").strip()[:500],
        "supplier_name": str(supplier.get("name") or "").strip()[:300],
        "available": bool(str(raw.get("title_zh") or "").strip() and skus),
        "stock_status": stock_status,
        "sku_count": len(skus),
        "skus": skus,
    }


def compare_snapshots(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if previous.get("available") and not current.get("available"):
        changes.append({"type": "PRODUCT_UNAVAILABLE", "severity": "critical", "message": "1688 商品已失效或详情不完整"})
    if previous.get("supplier_name") != current.get("supplier_name"):
        changes.append({"type": "SUPPLIER_CHANGED", "severity": "warning", "message": "供应商名称发生变化"})
    old_stock = str(previous.get("stock_status") or "UNKNOWN")
    new_stock = str(current.get("stock_status") or "UNKNOWN")
    if old_stock != "OUT_OF_STOCK" and new_stock == "OUT_OF_STOCK":
        changes.append({"type": "OUT_OF_STOCK", "severity": "critical", "message": "全部 SKU 已断货"})
    elif old_stock == "OUT_OF_STOCK" and new_stock == "IN_STOCK":
        changes.append({"type": "RESTOCKED", "severity": "info", "message": "商品已经补货"})
    old_skus = previous.get("skus") or {}
    new_skus = current.get("skus") or {}
    if not isinstance(old_skus, Mapping) or not isinstance(new_skus, Mapping):
        return changes
    for sku_id in sorted(set(old_skus) - set(new_skus)):
        changes.append({"type": "SKU_REMOVED", "severity": "warning", "sku_id": sku_id, "message": f"SKU {sku_id} 已下架"})
    for sku_id in sorted(set(new_skus) - set(old_skus)):
        changes.append({"type": "SKU_ADDED", "severity": "info", "sku_id": sku_id, "message": f"新增 SKU {sku_id}"})
    for sku_id in sorted(set(old_skus) & set(new_skus)):
        old_price = old_skus[sku_id].get("purchase_price_cny")
        new_price = new_skus[sku_id].get("purchase_price_cny")
        if old_price is not None and new_price is not None and old_price != new_price:
            changes.append({"type": "PRICE_CHANGED", "severity": "warning", "sku_id": sku_id, "old_value": old_price, "new_value": new_price, "message": f"SKU {sku_id} 采购价由 ¥{old_price} 变为 ¥{new_price}"})
    return changes


class SupplierMonitorRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "products": {}, "events": []}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SupplierMonitorError("供应商监控文件无法读取") from exc
        if not isinstance(document, dict) or not isinstance(document.get("products"), dict) or not isinstance(document.get("events"), list):
            raise SupplierMonitorError("供应商监控文件格式无效")
        return document

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

    def record(self, pipeline_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            current = snapshot_from_raw(raw)
            existing = document["products"].get(pipeline_id) or {}
            previous = existing.get("snapshot") if isinstance(existing, Mapping) else None
            changes = compare_snapshots(previous, current) if isinstance(previous, Mapping) else []
            checked_at = _now()
            events = [
                {"pipeline_id": pipeline_id, "checked_at": checked_at, **change}
                for change in changes
            ]
            document["products"][pipeline_id] = {
                "pipeline_id": pipeline_id,
                "checked_at": checked_at,
                "last_changed_at": checked_at if changes else existing.get("last_changed_at"),
                "check_status": "CHANGED" if changes else ("BASELINE" if previous is None else "UNCHANGED"),
                "snapshot": current,
                "changes": changes,
                "error": "",
            }
            document["events"] = [*document["events"], *events][-1000:]
            self._write(document)
            return document["products"][pipeline_id]

    def record_error(self, pipeline_id: str, error: str) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            existing = document["products"].get(pipeline_id) or {}
            document["products"][pipeline_id] = {
                **existing,
                "pipeline_id": pipeline_id,
                "checked_at": _now(),
                "check_status": "CHECK_FAILED",
                "changes": [],
                "error": str(error)[:500],
            }
            self._write(document)
            return document["products"][pipeline_id]

    def status(self) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            products = list(document["products"].values())
            products.sort(key=lambda item: str(item.get("checked_at") or ""), reverse=True)
            return {
                "summary": {
                    "monitored": len(products),
                    "changed": sum(item.get("check_status") == "CHANGED" for item in products),
                    "out_of_stock": sum((item.get("snapshot") or {}).get("stock_status") == "OUT_OF_STOCK" for item in products),
                    "failed": sum(item.get("check_status") == "CHECK_FAILED" for item in products),
                },
                "products": products,
                "events": list(document["events"])[-100:][::-1],
            }
