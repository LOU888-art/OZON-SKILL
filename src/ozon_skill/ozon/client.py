"""Client for OZON description-category metadata APIs only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OzonApiError(RuntimeError):
    """Raised when OZON metadata cannot be fetched."""


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
        return payload

