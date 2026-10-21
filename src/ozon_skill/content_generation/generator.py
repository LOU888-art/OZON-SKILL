"""Generate Russian copy from verified product facts without changing OZON IDs."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .rich_content import build_rich_content
from .tags import format_ozon_hashtags


NO_BRAND_TITLE_RU = "без бренда"


def _attribute_by_concept(prelisting: Mapping[str, Any], needle: str) -> Mapping[str, Any]:
    for item in prelisting.get("ozon_attributes") or []:
        if isinstance(item, Mapping) and needle.lower() in str(
            item.get("attribute_name_ru") or ""
        ).lower().replace("ё", "е"):
            return item
    return {}


def _legal_dictionary_values(attribute: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for item in attribute.get("dictionary_matches") or []:
        if isinstance(item, Mapping) and item.get("dictionary_value"):
            result.append(str(item["dictionary_value"]))
    return result


def _source_attribute(source: Mapping[str, Any], *names: str) -> Any:
    for item in source.get("attributes_zh") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if any(key in name for key in names):
            return item.get("value")
    return None


def _brand(prelisting: Mapping[str, Any], source: Mapping[str, Any]) -> str | None:
    del prelisting, source
    return NO_BRAND_TITLE_RU


def _temperature_ru(source: Mapping[str, Any]) -> str | None:
    raw = _source_attribute(source, "保温时长", "保温时间")
    if not isinstance(raw, str):
        return None
    numbers = re.findall(r"\d+", raw)
    if len(numbers) >= 2:
        return f"{numbers[0]}–{numbers[1]} часов"
    if len(numbers) == 1:
        return f"до {numbers[0]} часов"
    return None


def _title_and_segments(
    prelisting: Mapping[str, Any], source: Mapping[str, Any]
) -> tuple[str, dict[str, str | None]]:
    candidates = prelisting.get("top_category_candidates") or []
    selected = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
    product_type = str(selected.get("type_name_ru") or "Товар").strip()
    volume_values = _legal_dictionary_values(_attribute_by_concept(prelisting, "объем"))
    volume = volume_values[0] if volume_values else ""
    material_source = _attribute_by_concept(prelisting, "материал").get("value")
    grade = " 304" if isinstance(material_source, str) and "304" in material_source else ""
    material_phrase = f"из нержавеющей стали{grade}"
    temperature = _temperature_ru(source)

    core = product_type
    key_attributes = " ".join(part for part in (volume, material_phrase) if part)
    if temperature:
        scenario = (
            f"сохраняет температуру {temperature}, для горячих и холодных напитков "
            "дома, в офисе, автомобиле и путешествиях"
        )
    else:
        scenario = (
            "для горячих и холодных напитков дома, в офисе, автомобиле, "
            "на прогулках и в путешествиях"
        )
    brand = _brand(prelisting, source)

    def compose() -> str:
        first = " ".join(part for part in (core, key_attributes) if part)
        return " — ".join(part for part in (first, scenario, brand) if part)

    title = compose()
    if len(title) > 200:
        scenario = "для дома, офиса, автомобиля, прогулок и поездок"
        title = compose()
    if len(title) > 200 and volume:
        key_attributes = volume
        title = compose()
    for addition in (
        "практичный выбор для комфортного ежедневного использования",
        "подходит для себя и в подарок близким",
        "удобно брать с собой каждый день",
    ):
        if len(title) >= 150:
            break
        candidate_scenario = f"{scenario}, {addition}"
        previous = scenario
        scenario = candidate_scenario
        candidate = compose()
        if len(candidate) <= 200:
            title = candidate
        else:
            scenario = previous
    if len(title) > 200:
        shortened = title[:200].rsplit(" ", 1)[0].rstrip(" ,—-")
        title = shortened
    return title, {
        "core_product_name": core,
        "key_attributes": key_attributes,
        "use_case_or_core_benefit": scenario,
        "brand": brand,
    }


def _title_validation(title: str, segments: Mapping[str, str | None]) -> dict[str, Any]:
    length = len(title)
    first_60 = title[:60]
    core = segments.get("core_product_name") or ""
    words = re.findall(r"[а-яё]+", title.lower())
    repeated_roots: dict[str, int] = {}
    for word in words:
        root = word[:7] if len(word) > 7 else word
        repeated_roots[root] = repeated_roots.get(root, 0) + 1
    excessive = {root: count for root, count in repeated_roots.items() if count >= 3}
    issues: list[str] = []
    if not 150 <= length <= 200:
        issues.append("标题长度必须为 150–200 个字符")
    if core and core not in first_60:
        issues.append("核心产品名不在前 60 个字符内")
    if excessive:
        issues.append("存在同一词根重复 3 次以上")
    return {
        "length": length,
        "first_60_characters": first_60,
        "core_keyword_in_first_60": bool(core and core in first_60),
        "repeated_roots_3_or_more": excessive,
        "status": "VALID" if not issues else "NEEDS_CONFIRMATION",
        "issues": issues,
    }


def _description(prelisting: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    volume = _legal_dictionary_values(_attribute_by_concept(prelisting, "объем"))
    material = _legal_dictionary_values(_attribute_by_concept(prelisting, "материал"))
    colors = _legal_dictionary_values(_attribute_by_concept(prelisting, "цвет"))
    temperature = _temperature_ru(source)
    first = "Термокружка"
    if volume:
        first += f" объемом {volume[0]}"
    if material:
        first += f" изготовлена из материала «{material[0]}»"
    first += "."
    paragraphs = [first]
    if temperature:
        paragraphs.append(
            f"По данным поставщика, изделие сохраняет температуру напитка {temperature}."
        )
    if colors:
        paragraphs.append(f"Доступные варианты цвета: {', '.join(colors)}.")
    paragraphs.append(
        "Формат подходит для напитков дома и в офисе, а также для автомобиля, прогулок и поездок."
    )
    paragraphs.append(
        "Перед заказом выберите нужный вариант цвета и проверьте характеристики выбранной модели."
    )
    return "\n\n".join(paragraphs)


def _tags() -> tuple[list[str], dict[str, list[str]]]:
    groups = {
        "primary": [
            "термокружка",
            "термокружка 500 мл",
            "кружка из нержавеющей стали",
            "термокружка из стали 304",
            "кружка с сохранением температуры",
        ],
        "long_tail": [
            "дорожная термокружка",
            "кружка для офиса",
            "кружка для автомобиля",
            "кружка для горячих напитков",
            "кружка для холодных напитков",
            "термокружка на 6–12 часов",
            "кружка для путешествий",
            "многоразовая кружка",
        ],
        "supporting": [
            "подарок путешественнику",
            "подарок автомобилисту",
            "аксессуар для офиса",
            "посуда для поездок",
            "для мужчин и женщин",
        ],
    }
    flattened: list[str] = []
    seen: set[str] = set()
    for values in groups.values():
        for value in values:
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                flattened.append(value)
    return flattened[:30], groups


def _rich_content(
    media: Sequence[Mapping[str, Any]], description: str
) -> dict[str, Any] | None:
    urls: list[str] = []
    for item in media:
        url = item.get("final_public_url")
        usage = item.get("image_usage")
        if usage in {"shared_main", "sku_main"}:
            continue
        if (
            isinstance(url, str)
            and url not in urls
            and item.get("ocr_status") in {"COMPLETED", "REVIEWED"}
            and item.get("translation_status")
            in {"NOT_REQUIRED", "TRANSLATED_AND_UPLOADED", "AI_GENERATED"}
        ):
            urls.append(url)
    if not urls:
        return None
    block_titles = [
        "Подробности товара",
        "Характеристики и преимущества",
        "Использование и детали",
    ]
    summary = description.split("\n\n")[0]
    blocks = [
        {"url": url, "title": block_titles[index % len(block_titles)], "text": summary}
        for index, url in enumerate(urls[:20])
    ]
    return build_rich_content(blocks)


def _bind_generated_attributes(
    attributes: Sequence[Mapping[str, Any]],
    description: str,
    tags: Sequence[str],
    rich_content: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind copy only to attributes returned by OZON in this mapping run."""

    result = deepcopy(list(attributes))
    changes: list[dict[str, Any]] = []
    for item in result:
        if item.get("is_dictionary"):
            continue
        name = str(item.get("attribute_name_ru") or "").lower().replace("ё", "е")
        generated_field: str | None = None
        value: Any = None
        if "описание" in name or "аннотац" in name:
            generated_field, value = "description_ru", description
        elif "тег" in name or "ключев" in name:
            generated_field, value = "tags_ru", format_ozon_hashtags(tags)
        elif ("rich" in name or "рич" in name) and "контент" in name and rich_content:
            generated_field = "rich_content_json"
            value = json.dumps(rich_content, ensure_ascii=False, separators=(",", ":"))
        if generated_field is None:
            continue
        item["value"] = value
        item["mapping_status"] = "MAPPED_GENERATED"
        item["mapping_confidence"] = 1.0
        item["source_refs"] = [{"location": generated_field, "value": value}]
        changes.append(
            {
                "attribute_id": item.get("attribute_id"),
                "attribute_name_ru": item.get("attribute_name_ru"),
                "source_field": generated_field,
            }
        )
    return result, changes


def build_final_listing(
    prelisting: Mapping[str, Any],
    source: Mapping[str, Any],
    media_inspection: Sequence[Mapping[str, Any]],
    generated_copy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge copy with pre-listing attributes while preserving unresolved facts."""

    if generated_copy is None:
        title, segments = _title_and_segments(prelisting, source)
        description = _description(prelisting, source)
        tags, tag_groups = _tags()
        copy_provider = "BUILT_IN"
    else:
        title = str(generated_copy.get("title_ru") or "").strip()
        raw_segments = generated_copy.get("title_segments")
        if not isinstance(raw_segments, Mapping):
            raw_segments = {}
        segments = {
            "core_product_name": str(raw_segments.get("core_product_name") or "").strip(),
            "key_attributes": str(raw_segments.get("key_attributes") or "").strip() or None,
            "use_case_or_core_benefit": str(
                raw_segments.get("use_case_or_core_benefit") or ""
            ).strip()
            or None,
            "brand": NO_BRAND_TITLE_RU,
        }
        description = str(generated_copy.get("description_ru") or "").strip()
        raw_tags = generated_copy.get("tags_ru")
        tags = []
        seen: set[str] = set()
        if isinstance(raw_tags, list):
            for value in raw_tags:
                tag = str(value).strip()
                key = tag.casefold()
                if tag and key not in seen:
                    seen.add(key)
                    tags.append(tag)
        tags = tags[:30]
        tag_groups = {"ai_generated": tags}
        copy_provider = "AI_RELAY"
    title_validation = _title_validation(title, segments)
    rich_content = _rich_content(media_inspection, description)
    attributes, attribute_changes = _bind_generated_attributes(
        prelisting.get("ozon_attributes") or [], description, tags, rich_content
    )

    confirmations = deepcopy(prelisting.get("needs_confirmation") or [])
    unresolved_media = [
        item for item in media_inspection if not item.get("final_public_url")
    ]
    if unresolved_media:
        confirmations.append(
            {
                "field": "rich_content_json",
                "reason": "存在尚未完成下载、中文 OCR、翻译或公开托管的图片",
                "unresolved_image_count": len(unresolved_media),
            }
        )
    modified_images = [
        {
            "source_url": item.get("source_url"),
            "final_public_url": item.get("final_public_url"),
        }
        for item in media_inspection
        if item.get("contains_chinese") is True
        and item.get("translation_status")
        in {"TRANSLATED_AND_UPLOADED", "AI_GENERATED"}
    ]
    required_attribute_ids = {
        item.get("attribute_id")
        for item in attributes
        if isinstance(item, Mapping)
        and item.get("is_required")
        and isinstance(item.get("attribute_id"), int)
    }
    blocking_confirmations = [
        item
        for item in confirmations
        if isinstance(item, Mapping)
        and (
            item.get("attribute_id") in required_attribute_ids
            or item.get("field") == "ozon_category_id"
        )
    ]
    blockers = bool(
        blocking_confirmations
        or rich_content is None
        or title_validation["status"] != "VALID"
    )
    return {
        "schema_version": "1.0",
        "source_offer_id": prelisting.get("source_offer_id"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "listing_readiness": "BLOCKED" if blockers else "READY_FOR_PAYLOAD_ASSEMBLY",
        "ozon_category_id": prelisting.get("ozon_category_id"),
        "ozon_type_id": prelisting.get("ozon_type_id"),
        "ozon_attributes": attributes,
        "attribute_changes": attribute_changes,
        "title_ru": title,
        "title_segments": segments,
        "title_validation": title_validation,
        "description_ru": description,
        "copy_provider": copy_provider,
        "tags_ru": tags,
        "tag_groups": tag_groups,
        "rich_content_json": rich_content,
        "image_processing": list(media_inspection),
        "ocr_translated_images": modified_images,
        "needs_confirmation": confirmations,
        "blocking_confirmations": blocking_confirmations,
    }

