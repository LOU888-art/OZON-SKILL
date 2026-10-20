"""Assemble /v3/product/import payloads without performing network calls."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .config import DimensionDefaults, StoreConfig


@dataclass(frozen=True)
class AssemblyResult:
    payload: dict[str, Any]
    offer_ids: list[str]
    validation_errors: list[str]
    warnings: list[str]
    dimension_source: str | None
    required_attribute_ids: list[int]


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _offer_id(store_id: str, source_offer_id: str, sku_id: str | None) -> str:
    parts = [store_id, source_offer_id]
    if sku_id:
        parts.append(sku_id)
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(parts)).strip("-")
    if len(value) <= 50:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{value[:39]}-{digest}"


def _positive_number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return math.ceil(result)


def _dimensions(
    source: Mapping[str, Any], defaults: DimensionDefaults | None
) -> tuple[dict[str, Any] | None, str | None]:
    candidates: list[Mapping[str, Any]] = []
    for key in ("dimensions", "package_dimensions", "shipping_dimensions"):
        value = source.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    for candidate in candidates:
        depth = _positive_number(candidate.get("depth") or candidate.get("length"))
        width = _positive_number(candidate.get("width"))
        height = _positive_number(candidate.get("height"))
        weight = _positive_number(candidate.get("weight"))
        if all(item is not None for item in (depth, width, height, weight)):
            return (
                {
                    "depth": depth,
                    "width": width,
                    "height": height,
                    "weight": weight,
                    "dimension_unit": str(candidate.get("dimension_unit") or "mm"),
                    "weight_unit": str(candidate.get("weight_unit") or "g"),
                },
                "source_structured",
            )
    if defaults is None:
        return None, None
    return (
        {
            "depth": math.ceil(defaults.depth),
            "width": math.ceil(defaults.width),
            "height": math.ceil(defaults.height),
            "weight": math.ceil(defaults.weight),
            "dimension_unit": defaults.dimension_unit,
            "weight_unit": defaults.weight_unit,
        },
        "estimated_from_store_config",
    )


def _images(final: Mapping[str, Any], sku_id: str | None = None) -> list[str]:
    specific: list[str] = []
    shared_main: list[str] = []
    shared_gallery: list[str] = []
    legacy: list[str] = []
    for item in final.get("image_processing") or []:
        if not isinstance(item, Mapping):
            continue
        url = item.get("final_public_url")
        if not isinstance(url, str) or not url:
            continue
        usage = item.get("image_usage")
        if usage == "rich_content":
            continue
        if usage == "sku_main":
            if sku_id is not None and str(item.get("sku_id")) == sku_id:
                specific.append(url)
        elif usage == "shared_main":
            shared_main.append(url)
        elif usage == "shared_gallery":
            shared_gallery.append(url)
        elif usage is None:
            legacy.append(url)
    # A SKU-specific main replaces the shared first image; modules 2–8 remain shared.
    ordered = (
        specific + shared_gallery + legacy
        if specific
        else shared_main + shared_gallery + legacy
    )
    return list(dict.fromkeys(ordered))


def _api_attributes(
    attributes: Sequence[Mapping[str, Any]],
    sku: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[int]]:
    result: list[dict[str, Any]] = []
    errors: list[str] = []
    required_ids: list[int] = []
    for attribute in attributes:
        attribute_id = attribute.get("attribute_id")
        if not isinstance(attribute_id, int):
            continue
        required = bool(attribute.get("is_required"))
        if required:
            required_ids.append(attribute_id)
        value = attribute.get("value")
        dictionary = bool(attribute.get("is_dictionary"))
        values: list[dict[str, Any]] = []
        if dictionary:
            matches = attribute.get("dictionary_matches") or []
            source_refs = [
                item
                for item in attribute.get("source_refs") or []
                if isinstance(item, Mapping)
            ]
            sku_scoped = bool(source_refs) and all(
                str(item.get("location") or "").startswith("sku_list[")
                for item in source_refs
            )
            sku_values = {
                item.get("value")
                for item in (sku or {}).get("specification_zh") or []
                if isinstance(item, Mapping) and item.get("value") is not None
            }
            for match in matches:
                if not isinstance(match, Mapping):
                    continue
                if sku_scoped and match.get("source_value") not in sku_values:
                    continue
                dictionary_id = match.get("dictionary_value_id")
                legal_value = match.get("dictionary_value")
                if isinstance(dictionary_id, int):
                    entry: dict[str, Any] = {"dictionary_value_id": dictionary_id}
                    if legal_value is not None:
                        entry["value"] = str(legal_value)
                    values.append(entry)
            if value is not None and not values:
                errors.append(
                    f"dictionary attribute {attribute_id} has a value but no dictionary_value_id"
                )
        elif value is not None:
            raw_values = value if isinstance(value, list) else [value]
            values = [{"value": str(item)} for item in raw_values if item is not None]

        if values:
            result.append({"complex_id": 0, "id": attribute_id, "values": values})
        elif required:
            errors.append(f"required attribute {attribute_id} has no mapped value")
    return result, errors, required_ids


def _sku_rows(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = [item for item in source.get("sku_list") or [] if isinstance(item, Mapping)]
    return rows or [{}]


def _has_named_attribute(attributes: Sequence[Mapping[str, Any]], *needles: str) -> bool:
    for attribute in attributes:
        name = str(attribute.get("attribute_name_ru") or "").lower().replace("ё", "е")
        if any(needle in name for needle in needles):
            return True
    return False


def assemble_import_payload(
    final: Mapping[str, Any], source: Mapping[str, Any], store: StoreConfig
) -> AssemblyResult:
    """Build a dry-run payload even when validation reports execution blockers."""

    errors: list[str] = []
    warnings: list[str] = []
    source_offer_id = str(final.get("source_offer_id") or source.get("source_offer_id") or "")
    title = final.get("title_ru")
    type_id = final.get("ozon_type_id")
    category_id = final.get("ozon_category_id")
    if not source_offer_id:
        errors.append("source_offer_id is missing")
    if not isinstance(title, str) or not title:
        errors.append("title_ru is missing")
    if not isinstance(type_id, int):
        errors.append("ozon_type_id is missing")
    if not isinstance(category_id, int):
        errors.append("ozon_category_id is missing")
    if final.get("listing_readiness") != "READY_FOR_PAYLOAD_ASSEMBLY":
        errors.append("ozon-listing-final.json is not READY_FOR_PAYLOAD_ASSEMBLY")
    if final.get("rich_content_json") is None:
        errors.append("rich_content_json is not ready")

    dimensions, dimension_source = _dimensions(source, store.dimension_defaults)
    if dimensions is None:
        errors.append("dimensions and weight are missing and no estimate is configured")
        dimensions = {
            "depth": None,
            "width": None,
            "height": None,
            "weight": None,
            "dimension_unit": "mm",
            "weight_unit": "g",
        }
    elif dimension_source == "estimated_from_store_config":
        warnings.append("dimensions and weight use explicit store-config estimates")

    raw_attributes = [
        item for item in final.get("ozon_attributes") or [] if isinstance(item, Mapping)
    ]
    if final.get("description_ru") and not _has_named_attribute(
        raw_attributes, "описание", "аннотац"
    ):
        errors.append(
            "description_ru has no dynamically fetched OZON attribute binding"
        )
    if final.get("tags_ru") and not _has_named_attribute(
        raw_attributes, "тег", "ключев"
    ):
        errors.append("tags_ru has no dynamically fetched OZON attribute binding")
    if final.get("rich_content_json") is not None and not _has_named_attribute(
        raw_attributes, "rich", "контент"
    ):
        errors.append(
            "rich_content_json has no dynamically fetched OZON attribute binding"
        )
    required_ids = [
        item["attribute_id"]
        for item in raw_attributes
        if item.get("is_required") and isinstance(item.get("attribute_id"), int)
    ]

    items: list[dict[str, Any]] = []
    offer_ids: list[str] = []
    for sku in _sku_rows(source):
        attributes, attribute_errors, _ = _api_attributes(raw_attributes, sku)
        errors.extend(attribute_errors)
        sku_id_value = sku.get("sku_id")
        sku_id = str(sku_id_value) if sku_id_value is not None else None
        images = _images(final, sku_id)
        if not images:
            errors.append(
                f"SKU {sku_id or 'default'} has no verified public product images"
            )
        offer_id = _offer_id(store.store_id, source_offer_id, sku_id)
        offer_ids.append(offer_id)
        purchase_price = sku.get("purchase_price_cny")
        try:
            cny = Decimal(str(purchase_price))
            if not cny.is_finite() or cny <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            errors.append(f"SKU {sku_id or 'default'} has no valid CNY purchase price")
            price = Decimal("0")
        else:
            price = cny * store.cny_to_rub_rate
        item = {
            "offer_id": offer_id,
            "name": title,
            "price": _money(price),
            "old_price": _money(price * 2),
            "currency_code": store.currency_code,
            "description_category_id": category_id,
            "type_id": type_id,
            "attributes": attributes,
            "complex_attributes": [],
            "images": images,
            "primary_image": images[0] if images else "",
            "depth": dimensions["depth"],
            "width": dimensions["width"],
            "height": dimensions["height"],
            "dimension_unit": dimensions["dimension_unit"],
            "weight": dimensions["weight"],
            "weight_unit": dimensions["weight_unit"],
            "vat": store.vat,
        }
        # Inventory fields are deliberately absent from the import schema.
        items.append(item)
    return AssemblyResult(
        payload={"items": items},
        offer_ids=offer_ids,
        validation_errors=list(dict.fromkeys(errors)),
        warnings=warnings,
        dimension_source=dimension_source,
        required_attribute_ids=required_ids,
    )

