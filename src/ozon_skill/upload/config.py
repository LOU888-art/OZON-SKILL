"""Non-secret store configuration for payload assembly."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


@dataclass(frozen=True)
class DimensionDefaults:
    depth: int
    width: int
    height: int
    weight: int
    dimension_unit: str = "mm"
    weight_unit: str = "g"


@dataclass(frozen=True)
class StoreConfig:
    store_id: str
    credential_prefix: str
    cny_to_rub_rate: Decimal
    dimension_defaults: DimensionDefaults | None = None
    currency_code: str = "RUB"
    vat: str = "0"


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def load_store_configs(value: Mapping[str, Any]) -> list[StoreConfig]:
    raw_stores = value.get("stores")
    if not isinstance(raw_stores, list) or not raw_stores:
        raise ValueError("upload config must contain a non-empty stores array")
    result: list[StoreConfig] = []
    seen: set[str] = set()
    for raw in raw_stores:
        if not isinstance(raw, Mapping):
            raise ValueError("each store config must be an object")
        store_id = str(raw.get("store_id") or "").strip()
        prefix = str(raw.get("credential_prefix") or "").strip().upper()
        if not store_id or not prefix:
            raise ValueError("store_id and credential_prefix are required")
        if store_id in seen:
            raise ValueError(f"duplicate store_id: {store_id}")
        seen.add(store_id)
        defaults = raw.get("dimension_defaults")
        dimensions = None
        if defaults is not None:
            if not isinstance(defaults, Mapping):
                raise ValueError("dimension_defaults must be an object")
            dimensions = DimensionDefaults(
                depth=_positive_int(defaults.get("depth"), "depth"),
                width=_positive_int(defaults.get("width"), "width"),
                height=_positive_int(defaults.get("height"), "height"),
                weight=_positive_int(defaults.get("weight"), "weight"),
                dimension_unit=str(defaults.get("dimension_unit") or "mm"),
                weight_unit=str(defaults.get("weight_unit") or "g"),
            )
        result.append(
            StoreConfig(
                store_id=store_id,
                credential_prefix=prefix,
                cny_to_rub_rate=_positive_decimal(
                    raw.get("cny_to_rub_rate"), "cny_to_rub_rate"
                ),
                dimension_defaults=dimensions,
                currency_code=str(raw.get("currency_code") or "RUB").strip().upper(),
                vat=str(raw.get("vat") or "0"),
            )
        )
        if result[-1].currency_code not in {"RUB", "CNY"}:
            raise ValueError("currency_code must be RUB or CNY")
    return result

