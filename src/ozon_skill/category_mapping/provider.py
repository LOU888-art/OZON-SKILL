"""Live and fixture-backed providers for OZON category metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from ozon_skill.ozon.client import OzonDescriptionCategoryClient


class MetadataProvider(Protocol):
    mode: str
    calls: list[dict[str, Any]]

    def category_tree(self) -> dict[str, Any]: ...

    def category_attributes(self, category_id: int, type_id: int) -> dict[str, Any]: ...

    def attribute_values(
        self, category_id: int, type_id: int, attribute_id: int
    ) -> dict[str, Any]: ...


class _RecordingProvider:
    mode: str

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _record(
        self, endpoint: str, request: dict[str, Any], response: dict[str, Any]
    ) -> dict[str, Any]:
        canonical = json.dumps(
            response, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.calls.append(
            {
                "endpoint": endpoint,
                "request": request,
                "response_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        )
        return response


class LiveMetadataProvider(_RecordingProvider):
    mode = "live"

    def __init__(self, client: OzonDescriptionCategoryClient) -> None:
        super().__init__()
        self.client = client
        self._category_tree_cache: dict[str, Any] | None = None

    def category_tree(self) -> dict[str, Any]:
        if self._category_tree_cache is not None:
            return self._category_tree_cache
        request = {"language": "RU"}
        self._category_tree_cache = self._record(
            OzonDescriptionCategoryClient.TREE_PATH,
            request,
            self.client.category_tree(language="RU"),
        )
        return self._category_tree_cache

    def category_attributes(self, category_id: int, type_id: int) -> dict[str, Any]:
        request = {
            "description_category_id": category_id,
            "type_id": type_id,
            "language": "RU",
        }
        return self._record(
            OzonDescriptionCategoryClient.ATTRIBUTE_PATH,
            request,
            self.client.category_attributes(category_id, type_id, language="RU"),
        )

    def attribute_values(
        self, category_id: int, type_id: int, attribute_id: int
    ) -> dict[str, Any]:
        request = {
            "description_category_id": category_id,
            "type_id": type_id,
            "attribute_id": attribute_id,
            "language": "RU",
        }
        return self._record(
            OzonDescriptionCategoryClient.ATTRIBUTE_VALUES_PATH,
            request,
            self.client.attribute_values(category_id, type_id, attribute_id, language="RU"),
        )


class FixtureMetadataProvider(_RecordingProvider):
    mode = "mock"

    def __init__(self, fixture_directory: Path) -> None:
        super().__init__()
        self.fixture_directory = fixture_directory

    def _load(self, filename: str) -> dict[str, Any]:
        path = self.fixture_directory / filename
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"fixture {path} must contain a JSON object")
        return payload

    def category_tree(self) -> dict[str, Any]:
        request = {"language": "RU"}
        return self._record(
            OzonDescriptionCategoryClient.TREE_PATH,
            request,
            self._load("category_tree.json"),
        )

    def category_attributes(self, category_id: int, type_id: int) -> dict[str, Any]:
        request = {
            "description_category_id": category_id,
            "type_id": type_id,
            "language": "RU",
        }
        return self._record(
            OzonDescriptionCategoryClient.ATTRIBUTE_PATH,
            request,
            self._load("attributes.json"),
        )

    def attribute_values(
        self, category_id: int, type_id: int, attribute_id: int
    ) -> dict[str, Any]:
        request = {
            "description_category_id": category_id,
            "type_id": type_id,
            "attribute_id": attribute_id,
            "language": "RU",
        }
        return self._record(
            OzonDescriptionCategoryClient.ATTRIBUTE_VALUES_PATH,
            request,
            self._load(f"dictionary_{attribute_id}.json"),
        )

