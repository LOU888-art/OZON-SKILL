"""Client for OZON description-category metadata APIs only."""

from __future__ import annotations

import json
import hashlib
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OzonApiError(RuntimeError):
    """Raised when OZON metadata cannot be fetched."""


_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_METADATA_CACHE_LOCK = threading.Lock()
_METADATA_CACHE_MAX_ITEMS = 256


def _metadata_cache_ttl() -> float:
    try:
        value = float(os.environ.get("OZON_METADATA_CACHE_TTL_SECONDS", "900"))
    except ValueError:
        value = 900.0
    return max(0.0, min(value, 86400.0))


@dataclass(frozen=True)
class OzonDescriptionCategoryClient:
    """Read category, attribute, and dictionary metadata from OZON.

    This client intentionally has no product import methods.
    """

    client_id: str
    api_key: str
    base_url: str = "https://api-seller.ozon.ru"
    timeout_seconds: float = 30.0

    TREE_PATH = "/v1/description-category/tree"
    ATTRIBUTE_PATH = "/v1/description-category/attribute"
    ATTRIBUTE_VALUES_PATH = "/v1/description-category/attribute/values"

    def __post_init__(self) -> None:
        if not self.client_id:
            raise ValueError("client_id must not be empty")
        if not self.api_key:
            raise ValueError("api_key must not be empty")
        if not self.base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")

    def category_tree(self, *, language: str = "RU") -> dict[str, Any]:
        return self._post(self.TREE_PATH, {"language": language})

    def category_attributes(
        self,
        description_category_id: int,
        type_id: int,
        *,
        language: str = "RU",
    ) -> dict[str, Any]:
        return self._post(
            self.ATTRIBUTE_PATH,
            {
                "description_category_id": description_category_id,
                "type_id": type_id,
                "language": language,
            },
        )

    def attribute_values(
        self,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        *,
        language: str = "RU",
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Fetch every page of legal dictionary values for one attribute."""

        all_values: list[Any] = []
        last_value_id = 0
        while True:
            response = self._post(
                self.ATTRIBUTE_VALUES_PATH,
                {
                    "attribute_id": attribute_id,
                    "description_category_id": description_category_id,
                    "type_id": type_id,
                    "language": language,
                    "last_value_id": last_value_id,
                    "limit": limit,
                },
            )
            page = response.get("result")
            if isinstance(page, Mapping):
                values = page.get("values") or page.get("items") or []
                has_next = bool(page.get("has_next") or page.get("hasNext"))
            else:
                values = page or []
                has_next = bool(response.get("has_next") or response.get("hasNext"))
            if not isinstance(values, list):
                raise OzonApiError("OZON dictionary response has an unexpected shape")
            all_values.extend(values)
            if not has_next:
                break
            if not values:
                raise OzonApiError("OZON dictionary pagination did not advance")
            next_id = values[-1].get("id") if isinstance(values[-1], Mapping) else None
            if not isinstance(next_id, int) or next_id == last_value_id:
                raise OzonApiError("OZON dictionary pagination returned no usable last ID")
            last_value_id = next_id
        return {"result": all_values, "has_next": False}

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        ttl = _metadata_cache_ttl()
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "base_url": self.base_url.rstrip("/"),
                    "client_id": self.client_id,
                    "api_key_fingerprint": hashlib.sha256(
                        self.api_key.encode("utf-8")
                    ).hexdigest(),
                    "path": path,
                    "body": body,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        if ttl:
            with _METADATA_CACHE_LOCK:
                cached = _METADATA_CACHE.get(cache_key)
                if cached is not None and cached[0] > now:
                    return deepcopy(cached[1])
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Client-Id": self.client_id,
                "Api-Key": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ozon-skill-category-mapper/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise OzonApiError(f"OZON metadata API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise OzonApiError(f"OZON metadata request failed: {exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OzonApiError("OZON metadata API returned non-JSON content") from exc
        if not isinstance(payload, dict):
            raise OzonApiError("OZON metadata API returned an unexpected JSON shape")
        if payload.get("code") and not payload.get("result"):
            message = payload.get("message") or payload.get("details") or "unknown error"
            raise OzonApiError(f"OZON API error {payload['code']}: {message}")
        if ttl:
            with _METADATA_CACHE_LOCK:
                expired = [
                    key for key, (expires_at, _) in _METADATA_CACHE.items()
                    if expires_at <= now
                ]
                for key in expired:
                    _METADATA_CACHE.pop(key, None)
                if len(_METADATA_CACHE) >= _METADATA_CACHE_MAX_ITEMS:
                    oldest = next(iter(_METADATA_CACHE))
                    _METADATA_CACHE.pop(oldest, None)
                _METADATA_CACHE[cache_key] = (now + ttl, deepcopy(payload))
        return payload

