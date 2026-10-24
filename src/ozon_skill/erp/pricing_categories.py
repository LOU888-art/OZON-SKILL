"""Searchable Ozon RFBS category commissions extracted from the supplied workbook."""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def commission_catalog() -> dict[str, Any]:
    resource = files("ozon_skill.erp").joinpath("data/pricing_categories.json.gz")
    with gzip.open(resource, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    categories = document.get("categories")
    if not isinstance(categories, list):
        raise RuntimeError("Ozon 类目佣金数据格式无效")
    return document


@lru_cache(maxsize=1)
def _category_index() -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for item in commission_catalog()["categories"]
        if isinstance(item, dict) and item.get("id")
    }


def get_commission_category(identifier: Any) -> dict[str, Any] | None:
    item = _category_index().get(str(identifier or "").strip())
    return dict(item) if item else None


def search_commission_categories(query: Any, limit: Any = 30) -> dict[str, Any]:
    text = str(query or "").strip().casefold()
    try:
        normalized_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        normalized_limit = 30
    document = commission_catalog()
    if not text:
        matches: list[dict[str, Any]] = []
    else:
        tokens = [token for token in text.split() if token]
        matches = []
        for item in document["categories"]:
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("main_zh", "sub_zh", "type_zh", "main_ru", "sub_ru", "type_ru")
            ).casefold()
            if all(token in haystack for token in tokens):
                matches.append(dict(item))
                if len(matches) >= normalized_limit:
                    break
    return {
        "schema_version": document.get("schema_version", "1.0"),
        "effective_from": document.get("effective_from"),
        "price_bands_rub": document.get("price_bands_rub", [1500, 5000]),
        "total_categories": len(document["categories"]),
        "query": str(query or "").strip(),
        "results": matches,
    }
