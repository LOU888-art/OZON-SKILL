"""OZON Seller API methods used by the upload workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OzonUploadApiError(RuntimeError):
    """Raised when an upload-related OZON API request fails."""


@dataclass(frozen=True)
class OzonUploadClient:
    client_id: str
    api_key: str
    base_url: str = "https://api-seller.ozon.ru"
    timeout_seconds: float = 30.0

    IMPORT_PATH = "/v3/product/import"
    IMPORT_INFO_PATH = "/v1/product/import/info"
    ATTRIBUTES_UPDATE_PATH = "/v1/product/attributes/update"
    UNARCHIVE_PATH = "/v2/product/unarchive"
    INFO_LIST_PATH = "/v3/product/info/list"
    INFO_ATTRIBUTES_PATH = "/v4/product/info/attributes"

    def __post_init__(self) -> None:
        if not self.client_id or not self.api_key:
            raise ValueError("OZON Client-Id and Api-Key are required")
        if not self.base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")

    def product_import(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(self.IMPORT_PATH, payload)

    def import_info(self, task_id: int) -> dict[str, Any]:
        return self._post(self.IMPORT_INFO_PATH, {"task_id": task_id})

    def attributes_update(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post(self.ATTRIBUTES_UPDATE_PATH, {"items": items})

    def unarchive(self, product_ids: list[int]) -> dict[str, Any]:
        return self._post(self.UNARCHIVE_PATH, {"product_id": product_ids})

    def product_info_list(self, offer_ids: list[str]) -> dict[str, Any]:
        return self._post(
            self.INFO_LIST_PATH,
            {"offer_id": offer_ids, "product_id": [], "sku": []},
        )

    def product_info_attributes(self, offer_ids: list[str]) -> dict[str, Any]:
        return self._post(
            self.INFO_ATTRIBUTES_PATH,
            {"filter": {"offer_id": offer_ids}, "limit": 100, "last_id": ""},
        )

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
                "User-Agent": "ozon-skill-uploader/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                raw_error = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw_error = ""
            detail = raw_error.strip().replace("\n", " ")[:1200]
            suffix = f": {detail}" if detail else ""
            raise OzonUploadApiError(
                f"OZON returned HTTP {exc.code} for {path}{suffix}"
            ) from exc
        except URLError as exc:
            raise OzonUploadApiError(f"OZON request failed for {path}: {exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OzonUploadApiError(f"OZON returned non-JSON content for {path}") from exc
        if not isinstance(payload, dict):
            raise OzonUploadApiError(f"OZON returned an unexpected JSON shape for {path}")
        if payload.get("code") and payload.get("result") is None:
            message = payload.get("message") or "unknown API error"
            raise OzonUploadApiError(f"OZON API error {payload['code']}: {message}")
        return payload

