"""Read-only Ozon order and inventory API integration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OzonOperationsError(RuntimeError):
    """Raised when an Ozon operations read request fails."""


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def _rows(value: Mapping[str, Any], key: str = "postings") -> list[Mapping[str, Any]]:
    result = value.get("result")
    if isinstance(result, Mapping):
        candidates = result.get(key) or result.get("items") or []
    elif isinstance(result, list):
        candidates = result
    else:
        candidates = value.get(key) or value.get("items") or []
    return [item for item in candidates if isinstance(item, Mapping)] if isinstance(candidates, list) else []


def normalize_postings(value: Mapping[str, Any], scheme: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for posting in _rows(value):
        products = [item for item in posting.get("products") or [] if isinstance(item, Mapping)]
        quantity = sum(int(item.get("quantity") or 0) for item in products)
        revenue = sum((_decimal(item.get("price")) * int(item.get("quantity") or 0) for item in products), Decimal("0"))
        analytics = posting.get("analytics_data") if isinstance(posting.get("analytics_data"), Mapping) else {}
        normalized.append(
            {
                "posting_number": posting.get("posting_number") or posting.get("order_number") or "—",
                "order_id": posting.get("order_id"),
                "scheme": scheme,
                "status": posting.get("status") or "unknown",
                "substatus": posting.get("substatus") or posting.get("status_alias"),
                "shipment_date": posting.get("shipment_date") or posting.get("delivering_date"),
                "created_at": posting.get("in_process_at") or posting.get("created_at"),
                "warehouse": analytics.get("warehouse_name") or posting.get("warehouse_name") or "—",
                "product_count": len(products),
                "quantity": quantity,
                "revenue": str(revenue.quantize(Decimal("0.01"))),
                "currency_code": next((str(item.get("currency_code")) for item in products if item.get("currency_code")), "RUB"),
                "products": [
                    {
                        "offer_id": item.get("offer_id"),
                        "name": item.get("name"),
                        "quantity": int(item.get("quantity") or 0),
                        "price": str(item.get("price") or "0"),
                        "currency_code": item.get("currency_code") or "RUB",
                    }
                    for item in products
                ],
            }
        )
    return normalized


def normalize_stocks(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in _rows(value, "items"):
        stock_rows = [row for row in item.get("stocks") or [] if isinstance(row, Mapping)]
        present = int(item.get("present") or sum(int(row.get("present") or 0) for row in stock_rows))
        reserved = int(item.get("reserved") or sum(int(row.get("reserved") or 0) for row in stock_rows))
        normalized.append(
            {
                "offer_id": item.get("offer_id") or item.get("offer_code") or "—",
                "product_id": item.get("product_id"),
                "sku": item.get("sku"),
                "present": present,
                "reserved": reserved,
                "available": max(0, present - reserved),
                "warehouses": len(stock_rows),
            }
        )
    return normalized


@dataclass(frozen=True)
class OzonOperationsClient:
    client_id: str
    api_key: str
    base_url: str = "https://api-seller.ozon.ru"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.client_id or not self.api_key:
            raise ValueError("Ozon Client-Id and Api-Key are required")
        if not self.base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Client-Id": self.client_id,
                "Api-Key": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "northstar-operations/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:600]
            raise OzonOperationsError(f"Ozon {path} HTTP {exc.code}{': ' + detail if detail else ''}") from exc
        except URLError as exc:
            raise OzonOperationsError(f"Ozon {path} 请求失败：{exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OzonOperationsError(f"Ozon {path} 返回非 JSON") from exc
        if not isinstance(payload, dict):
            raise OzonOperationsError(f"Ozon {path} 返回格式异常")
        return payload

    def orders(self, days: int = 30, limit: int = 100) -> tuple[list[dict[str, Any]], list[str]]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        date_filter = {"since": since.isoformat().replace("+00:00", "Z"), "to": now.isoformat().replace("+00:00", "Z")}
        warnings: list[str] = []
        orders: list[dict[str, Any]] = []
        requests = [
            ("FBS", "/v3/posting/fbs/list", {"dir": "DESC", "filter": date_filter, "limit": limit, "offset": 0, "with": {"analytics_data": True, "financial_data": True}}),
            ("FBO", "/v2/posting/fbo/list", {"dir": "DESC", "filter": date_filter, "limit": limit, "offset": 0, "with": {"analytics_data": True, "financial_data": True}}),
        ]
        for scheme, path, body in requests:
            try:
                orders.extend(normalize_postings(self._post(path, body), scheme))
            except OzonOperationsError as exc:
                warnings.append(f"{scheme} 订单读取失败：{exc}")
        orders.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return orders, warnings

    def stocks(self, limit: int = 1000) -> list[dict[str, Any]]:
        response = self._post(
            "/v4/product/info/stocks",
            {"filter": {"visibility": "ALL"}, "last_id": "", "limit": limit},
        )
        return normalize_stocks(response)
