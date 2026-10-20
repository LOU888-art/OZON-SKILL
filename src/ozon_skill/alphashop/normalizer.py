"""Map AlphaShop offer-detail responses to the immutable stage-1 contract."""

from __future__ import annotations

from typing import Any, Mapping

from ozon_skill.alibaba1688.normalizer import normalize_product


def _raw_contract(normalized: Mapping[str, Any]) -> dict[str, Any]:
    raw_attributes = normalized.get("attributes_zh")
    attributes: dict[str, Any] = {}
    if isinstance(raw_attributes, list):
        attributes = {
            str(item.get("name")): item.get("value")
            for item in raw_attributes
            if isinstance(item, Mapping) and item.get("name") is not None
        }
    category = normalized.get("category_path_zh")
    category_id = category.get("selected_category_id") if isinstance(category, Mapping) else None
    category_path = category.get("path") if isinstance(category, Mapping) else category
    supplier = normalized.get("supplier_info")
    supplier = supplier if isinstance(supplier, Mapping) else {}
    return {
        "source_offer_id": normalized.get("source_offer_id"),
        "title_zh": normalized.get("title_zh"),
        "images": normalized.get("images") or [],
        "detail_images": normalized.get("detail_images") or [],
        "sku_list": normalized.get("sku_list") or [],
        "attributes_zh": attributes,
        "category_path_zh": category_path or [],
        "category_id": category_id,
        "supplier_info": {
            "name": supplier.get("company_name"),
            "min_order": supplier.get("minimum_order_quantity"),
            "ship_from": supplier.get("ship_from_zh"),
        },
    }


def normalize_alphashop_offer(
    response: Mapping[str, Any], requested_offer_id: str | int
) -> dict[str, Any]:
    normalized = normalize_product(
        response,
        requested_offer_id,
        collection_mode="live",
        source_api="alphashop.openclaw.offer.detail.query/1.0",
    )
    return _raw_contract(normalized)
