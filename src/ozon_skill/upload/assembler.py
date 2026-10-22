"""Assemble /v3/product/import payloads without performing network calls."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from ozon_skill.content_generation.rich_content import upgrade_legacy_rich_content
from ozon_skill.content_generation.tags import format_ozon_hashtags

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


_CJK = re.compile(r"[\u3400-\u9fff]")
_COLOR_TRANSLATIONS = (
    ("英国梨与小苍兰", "Английская груша и фрезия"),
    ("鼠尾草海盐", "Шалфей и морская соль"),
    ("白桃乌龙", "Белый персик и улун"),
    ("玫瑰檀香", "Роза и сандал"),
    ("一轮玫瑰", "Роза"),
    ("奶油蛋糕", "Сливочный торт"),
    ("迷雾森林", "Туманный лес"),
    ("大吉岭茶", "Дарджилингский чай"),
    ("乌木沉香", "Уд и агаровое дерево"),
    ("香格里拉", "Шангри-Ла"),
    ("蓝风铃", "Голубой колокольчик"),
    ("黑鸦片", "Черный опиум"),
    ("薰衣草", "Лаванда"),
    ("晚香玉", "Тубероза"),
    ("冷水", "Прохладная вода"),
    ("香草", "Ваниль"),
    ("米白", "Бежевый"),
    ("咖啡", "Коричневый"),
    ("透明", "Прозрачный"),
    ("彩色", "Разноцветный"),
    ("黑", "Черный"),
    ("白", "Белый"),
    ("红", "Красный"),
    ("粉", "Розовый"),
    ("蓝", "Синий"),
    ("绿", "Зеленый"),
    ("黄", "Желтый"),
    ("紫", "Фиолетовый"),
    ("灰", "Серый"),
    ("棕", "Коричневый"),
    ("橙", "Оранжевый"),
)


def _attribute_name(attribute: Mapping[str, Any]) -> str:
    return str(attribute.get("attribute_name_ru") or "").lower().replace("ё", "е")


def _is_color_attribute(attribute: Mapping[str, Any]) -> bool:
    return "цвет" in _attribute_name(attribute)


def _is_tag_attribute(attribute: Mapping[str, Any]) -> bool:
    name = _attribute_name(attribute)
    return "тег" in name or "хештег" in name or "ключев" in name


def _is_rich_content_attribute(attribute: Mapping[str, Any]) -> bool:
    name = _attribute_name(attribute)
    return ("rich" in name or "рич" in name) and "контент" in name


def _is_single_valued(attribute: Mapping[str, Any]) -> bool:
    max_count = attribute.get("max_count")
    return (
        attribute.get("is_collection") is False
        or max_count == 1
        or _is_color_attribute(attribute)
        or _is_tag_attribute(attribute)
        or _is_rich_content_attribute(attribute)
        or "бренд" in _attribute_name(attribute)
    )


def _sku_source_values(
    attribute: Mapping[str, Any], sku: Mapping[str, Any] | None
) -> list[Any]:
    value = attribute.get("value")
    raw_values = (
        list(value)
        if isinstance(value, list)
        else ([value] if value is not None else [])
    )
    source_refs = [
        item for item in attribute.get("source_refs") or [] if isinstance(item, Mapping)
    ]
    sku_values = {
        str(item.get("value")).strip()
        for item in (sku or {}).get("specification_zh") or []
        if isinstance(item, Mapping) and item.get("value") is not None
    }
    specification_text = str((sku or {}).get("specification_text_zh") or "")
    for part in specification_text.split(";"):
        candidate = part.split(":", 1)[-1].strip()
        if candidate:
            sku_values.add(candidate)
    matched_values = [
        item for item in raw_values if str(item).strip() in sku_values
    ]
    if matched_values:
        return matched_values
    sku_scoped = bool(source_refs) and all(
        str(item.get("location") or "").startswith("sku_list[") for item in source_refs
    )
    if not sku_scoped:
        return raw_values
    return [
        item.get("value")
        for item in source_refs
        if item.get("value") is not None
        and str(item.get("value")).strip() in sku_values
    ]


def _russian_color(value: Any) -> str:
    result = str(value or "").strip()
    if not _CJK.search(result):
        return result
    for token, translation in _COLOR_TRANSLATIONS:
        if len(token) > 1 and token in result:
            return translation
    translations = {
        translation
        for token, translation in _COLOR_TRANSLATIONS
        if len(token) == 1 and token in result
    }
    if len(translations) == 1:
        return translations.pop()
    return result


def _deduplicate_values(values: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _api_attributes(
    attributes: Sequence[Mapping[str, Any]],
    sku: Mapping[str, Any] | None = None,
    *,
    fallback_title: str = "Подробности товара",
    fallback_text: str = "Описание и преимущества товара",
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
            raw_values = _sku_source_values(attribute, sku)
            if _is_tag_attribute(attribute):
                tag_value = format_ozon_hashtags(
                    raw_values[0]
                    if len(raw_values) == 1 and isinstance(raw_values[0], str)
                    else raw_values
                )
                if not tag_value:
                    errors.append(f"hashtag attribute {attribute_id} has no valid #tags")
                else:
                    values = [{"value": tag_value}]
            elif _is_rich_content_attribute(attribute):
                if len(raw_values) != 1:
                    errors.append(
                        f"rich-content attribute {attribute_id} must contain exactly one JSON value"
                    )
                elif raw_values:
                    try:
                        rich_value = (
                            json.loads(raw_values[0])
                            if isinstance(raw_values[0], str)
                            else raw_values[0]
                        )
                        rich_value = upgrade_legacy_rich_content(
                            rich_value,
                            fallback_title=fallback_title,
                            fallback_text=fallback_text,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        errors.append(
                            f"rich-content attribute {attribute_id} is invalid: {exc}"
                        )
                    else:
                        values = [
                            {
                                "value": json.dumps(
                                    rich_value,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            }
                        ]
            else:
                if _is_color_attribute(attribute):
                    raw_values = [_russian_color(item) for item in raw_values]
                values = [
                    {"value": str(item).strip()}
                    for item in raw_values
                    if item is not None and str(item).strip()
                ]

        values = _deduplicate_values(values)
        if _is_color_attribute(attribute):
            for entry in values:
                color = str(entry.get("value") or "")
                if not color and isinstance(entry.get("dictionary_value_id"), int):
                    continue
                if _CJK.search(color) or not re.search(r"[А-Яа-яЁё]", color):
                    errors.append(
                        f"color attribute {attribute_id} must contain one Russian "
                        f"value, got {color!r}"
                    )
        if _is_single_valued(attribute) and len(values) > 1:
            errors.append(
                f"single-value attribute {attribute_id} ({attribute.get('attribute_name_ru')}) "
                f"contains {len(values)} values"
            )
            values = values[:1]

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
    rich_fallback_text = str(final.get("description_ru") or "Описание товара")
    rich_fallback_text = rich_fallback_text.split("\n\n", 1)[0][:500]
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
    else:
        try:
            upgrade_legacy_rich_content(
                final.get("rich_content_json"),
                fallback_title="Подробности товара",
                fallback_text=rich_fallback_text,
            )
        except ValueError as exc:
            errors.append(f"rich_content_json is invalid: {exc}")

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
        attributes, attribute_errors, _ = _api_attributes(
            raw_attributes,
            sku,
            fallback_title="Подробности товара",
            fallback_text=rich_fallback_text,
        )
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
        sale_price = sku.get("sale_price_cny")
        try:
            cny = Decimal(str(purchase_price))
            if not cny.is_finite() or cny <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            errors.append(f"SKU {sku_id or 'default'} has no valid CNY purchase price")
            price = Decimal("0")
        else:
            try:
                sale_cny = Decimal(str(sale_price)) if sale_price not in (None, "") else cny
                if not sale_cny.is_finite() or sale_cny <= 0:
                    raise ValueError
            except (ValueError, ArithmeticError):
                errors.append(f"SKU {sku_id or 'default'} has no valid CNY sale price")
                price = Decimal("0")
            else:
                price = sale_cny * store.cny_to_rub_rate
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

