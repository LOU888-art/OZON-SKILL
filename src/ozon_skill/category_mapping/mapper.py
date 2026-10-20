"""Auditable mapping from normalized 1688 data to live OZON metadata."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .provider import MetadataProvider


CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class Concept:
    name: str
    zh_terms: tuple[str, ...]
    ru_terms: tuple[str, ...]


# Semantic aliases contain words only. No OZON identifiers are stored here.
CATEGORY_CONCEPTS = (
    Concept("thermos", ("保温杯", "保温", "真空杯"), ("термос", "термокруж")),
    Concept("water_bottle", ("水杯", "水瓶", "运动水壶"), ("бутылк", "для воды")),
    Concept("mug", ("杯具", "马克杯"), ("кружк", "чашк")),
)

ATTRIBUTE_CONCEPTS = (
    Concept("material", ("材质", "材料"), ("материал",)),
    Concept("color", ("颜色", "色彩"), ("цвет",)),
    Concept("volume", ("容量", "容积"), ("объем", "объём")),
    Concept("size", ("尺寸", "大小", "规格"), ("размер",)),
    Concept("brand", ("品牌",), ("бренд", "торговая марка")),
    Concept("country", ("产地", "原产地"), ("страна производства",)),
)

VALUE_CONCEPTS = (
    Concept("stainless_steel", ("不锈钢", "304不锈钢", "316不锈钢"), ("нержавеющая сталь",)),
    Concept("black", ("黑色", "曜石黑", "黑"), ("черный", "чёрный")),
    Concept("white", ("白色", "珍珠白", "白"), ("белый",)),
    Concept("blue", ("蓝色", "蓝"), ("синий", "голубой")),
)


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().replace("ё", "е")
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", text).strip()


def _contains(text: str, term: str) -> bool:
    return _normalized(term) in _normalized(text)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_list(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = response.get("result", [])
    if isinstance(result, Mapping):
        result = result.get("items") or result.get("values") or result.get("attributes") or []
    return [item for item in result if isinstance(item, Mapping)] if isinstance(result, list) else []


def _category_source_text(source: Mapping[str, Any]) -> tuple[str, str, str]:
    title = str(source.get("title_zh") or "")
    category = source.get("category_path_zh")
    path_names: list[str] = []
    if isinstance(category, Mapping):
        for item in category.get("path") or []:
            if isinstance(item, Mapping) and item.get("name_zh"):
                path_names.append(str(item["name_zh"]))
    path = " / ".join(path_names)
    leaf = path_names[-1] if path_names else ""
    return title, path, leaf


def _flatten_categories(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    def sequence(value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            if any(
                key in value
                for key in (
                    "description_category_id",
                    "descriptionCategoryId",
                    "category_id",
                    "categoryId",
                    "type_id",
                    "typeId",
                )
            ):
                return [value]
            for key in (
                "items",
                "categories",
                "children",
                "result",
                "data",
                "category_tree",
                "categoryTree",
            ):
                nested = value.get(key)
                if nested is not None:
                    found = sequence(nested)
                    if found:
                        return found
            # OZON has changed protobuf/JSON wrapper names before. Traverse an
            # unknown wrapper without interpreting any scalar as an ID.
            for nested in value.values():
                if isinstance(nested, (list, Mapping)):
                    found = sequence(nested)
                    if found:
                        return found
        return []

    def field(node: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if node.get(name) is not None:
                return node[name]
        return None

    def is_disabled(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return bool(value)

    roots = sequence(response.get("result", response))
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()

    def walk(node: Mapping[str, Any], parent_names: list[str]) -> None:
        name = str(field(node, "category_name", "categoryName", "name") or "").strip()
        path = parent_names + ([name] if name else [])
        category_id = _as_int(field(
            node,
            "description_category_id",
            "descriptionCategoryId",
            "category_id",
            "categoryId",
        ))
        type_data = node.get("type") if isinstance(node.get("type"), Mapping) else {}
        type_id = _as_int(field(node, "type_id", "typeId") or field(type_data, "id", "type_id", "typeId"))
        disabled = is_disabled(field(node, "disabled", "is_disabled", "isDisabled"))
        children = sequence(field(
            node,
            "children",
            "child_categories",
            "childCategories",
            "subcategories",
            "subCategories",
        ))
        if (
            category_id is not None
            and type_id not in (None, 0)
            and not disabled
            and not children
            and (category_id, type_id) not in seen
        ):
            seen.add((category_id, type_id))
            candidates.append(
                {
                    "ozon_category_id": category_id,
                    "ozon_type_id": type_id,
                    "category_name_ru": name,
                    "category_path_ru": path,
                    "type_name_ru": field(node, "type_name", "typeName") or field(type_data, "name", "type_name", "typeName"),
                }
            )
        types = sequence(field(node, "types", "category_types", "categoryTypes"))
        if category_id is not None and not disabled and not children:
            for item in types:
                nested_type_id = _as_int(field(item, "type_id", "typeId", "id"))
                pair = (category_id, nested_type_id) if nested_type_id is not None else None
                if (
                    nested_type_id not in (None, 0)
                    and not is_disabled(field(item, "disabled", "is_disabled", "isDisabled"))
                    and pair not in seen
                ):
                    seen.add((category_id, nested_type_id))
                    candidates.append(
                        {
                            "ozon_category_id": category_id,
                            "ozon_type_id": nested_type_id,
                            "category_name_ru": name,
                            "category_path_ru": path,
                            "type_name_ru": field(item, "type_name", "typeName", "name"),
                        }
                    )
        for child in children:
            walk(child, path)

    for root in roots:
        walk(root, [])
    return candidates


def _category_tree_shape(response: Mapping[str, Any]) -> str:
    """Return a credential-free structural summary for actionable errors."""

    mapping_count = 0
    list_count = 0
    category_nodes = 0
    nonzero_type_nodes = 0
    enabled_type_nodes = 0
    enabled_leaf_type_nodes = 0
    disabled_true_nodes = 0
    disabled_false_nodes = 0
    disabled_other_nodes = 0
    relevant_keys: set[str] = set()
    interesting = re.compile(r"category|type|child|disabled|result|item|data", re.I)

    def first(value: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in value:
                return value[name]
        return None

    def disabled_state(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no", ""}:
                return False
        if value is None:
            return False
        return None

    def has_children(value: Any) -> bool:
        if isinstance(value, list):
            return bool(value)
        if isinstance(value, Mapping):
            for key in ("items", "categories", "children", "result", "data"):
                if key in value:
                    return has_children(value[key])
            return bool(value)
        return False

    def visit(value: Any) -> None:
        nonlocal mapping_count, list_count, category_nodes, nonzero_type_nodes
        nonlocal enabled_type_nodes, enabled_leaf_type_nodes
        nonlocal disabled_true_nodes, disabled_false_nodes, disabled_other_nodes
        if isinstance(value, Mapping):
            mapping_count += 1
            category_id = first(
                value,
                "description_category_id",
                "descriptionCategoryId",
                "category_id",
                "categoryId",
            )
            if category_id is not None:
                category_nodes += 1
                type_id = _as_int(first(value, "type_id", "typeId"))
                state = disabled_state(
                    first(value, "disabled", "is_disabled", "isDisabled")
                )
                if state is True:
                    disabled_true_nodes += 1
                elif state is False:
                    disabled_false_nodes += 1
                else:
                    disabled_other_nodes += 1
                if type_id not in (None, 0):
                    nonzero_type_nodes += 1
                    if state is False:
                        enabled_type_nodes += 1
                        children = first(
                            value,
                            "children",
                            "child_categories",
                            "childCategories",
                            "subcategories",
                            "subCategories",
                        )
                        if not has_children(children):
                            enabled_leaf_type_nodes += 1
            for key, nested in value.items():
                key_text = str(key)
                if interesting.search(key_text):
                    relevant_keys.add(key_text)
                visit(nested)
        elif isinstance(value, list):
            list_count += 1
            for nested in value:
                visit(nested)

    visit(response)
    root_keys = sorted(str(key) for key in response.keys())[:12]
    observed = sorted(relevant_keys)[:24]
    return (
        f"root_keys={root_keys}; mappings={mapping_count}; lists={list_count}; "
        f"category_nodes={category_nodes}; nonzero_type_nodes={nonzero_type_nodes}; "
        f"enabled_type_nodes={enabled_type_nodes}; "
        f"enabled_leaf_type_nodes={enabled_leaf_type_nodes}; "
        f"disabled_true={disabled_true_nodes}; disabled_false={disabled_false_nodes}; "
        f"disabled_other={disabled_other_nodes}; observed_keys={observed}"
    )


def _score_category(
    candidate: Mapping[str, Any], title: str, path: str, leaf: str
) -> tuple[float, list[str]]:
    candidate_text = " ".join(
        [
            *(str(part) for part in candidate.get("category_path_ru") or []),
            str(candidate.get("type_name_ru") or ""),
        ]
    )
    score = 0.05
    reasons: list[str] = []
    for concept in CATEGORY_CONCEPTS:
        title_hit = any(_contains(title, term) for term in concept.zh_terms)
        path_hit = any(_contains(path, term) for term in concept.zh_terms)
        leaf_hit = any(_contains(leaf, term) for term in concept.zh_terms)
        candidate_hit = any(_contains(candidate_text, term) for term in concept.ru_terms)
        if not candidate_hit or not (title_hit or path_hit):
            continue
        concept_score = 0.54
        if title_hit:
            concept_score += 0.16
        if path_hit:
            concept_score += 0.12
        if leaf_hit:
            concept_score += 0.08
        score = max(score, concept_score)
        evidence: list[str] = []
        if title_hit:
            evidence.append("商品标题")
        if path_hit:
            evidence.append("1688 类目路径")
        reasons.append(
            f"{'+'.join(evidence)}与 OZON 类目共同命中语义概念 {concept.name}"
        )
    if not reasons:
        reasons.append("没有命中已审计的跨语言类目语义规则")
    return round(min(score, 0.98), 3), reasons


def _rank_categories(
    tree_response: Mapping[str, Any],
    source: Mapping[str, Any],
    preferred_pair: tuple[int, int] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    title, path, leaf = _category_source_text(source)
    ranked: list[dict[str, Any]] = []
    for candidate in _flatten_categories(tree_response):
        score, reasons = _score_category(candidate, title, path, leaf)
        if preferred_pair == (
            candidate["ozon_category_id"],
            candidate["ozon_type_id"],
        ):
            score = 0.99
            reasons = [
                "AlphaShop 类目映射返回该 category/type，且已在本次 OZON 类目树中验证",
                *reasons,
            ]
        ranked.append({**candidate, "confidence": score, "matching_reasons": reasons})
    ranked.sort(key=lambda item: (-item["confidence"], item["ozon_category_id"], item["ozon_type_id"]))
    matched = any(
        preferred_pair == (item["ozon_category_id"], item["ozon_type_id"])
        for item in ranked
    )
    return ranked[:3], matched


def _attribute_concept(name_ru: str) -> Concept | None:
    for concept in ATTRIBUTE_CONCEPTS:
        if any(_contains(name_ru, term) for term in concept.ru_terms):
            return concept
    return None


def _source_attribute_values(
    source: Mapping[str, Any], concept: Concept
) -> tuple[list[Any], list[dict[str, Any]]]:
    values: list[Any] = []
    references: list[dict[str, Any]] = []

    for item in source.get("attributes_zh") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if any(_contains(name, term) for term in concept.zh_terms):
            value = item.get("value")
            if value is not None and value not in values:
                values.append(value)
                references.append(
                    {"location": "attributes_zh", "name": name, "value": value}
                )

    for sku_index, sku in enumerate(source.get("sku_list") or []):
        if not isinstance(sku, Mapping):
            continue
        for item in sku.get("specification_zh") or []:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if any(_contains(name, term) for term in concept.zh_terms):
                value = item.get("value")
                if value is not None and value not in values:
                    values.append(value)
                    references.append(
                        {
                            "location": f"sku_list[{sku_index}].specification_zh",
                            "name": name,
                            "value": value,
                        }
                    )
    return values, references


def _dictionary_items(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _result_list(response)


def _numeric_tokens(value: Any) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+(?:[.,]\d+)?", _normalized(value)))


def _dictionary_score(source_value: Any, dictionary_value: Any) -> tuple[float, str]:
    source_text = _normalized(source_value)
    target_text = _normalized(dictionary_value)
    if source_text and source_text == target_text:
        return 1.0, "标准化文本完全一致"
    for concept in VALUE_CONCEPTS:
        source_hit = any(_contains(source_text, term) for term in concept.zh_terms)
        target_hit = any(_contains(target_text, term) for term in concept.ru_terms)
        if source_hit and target_hit:
            return 0.96, f"共同命中受控值语义 {concept.name}"
    source_numbers = _numeric_tokens(source_text)
    target_numbers = _numeric_tokens(target_text)
    if source_numbers and source_numbers == target_numbers:
        source_is_volume = "ml" in source_text or "毫升" in source_text or "мл" in source_text
        target_is_volume = "ml" in target_text or "毫升" in target_text or "мл" in target_text
        if source_is_volume and target_is_volume:
            return 0.90, "容量数值和单位一致"
    return 0.0, "没有可靠的字典值对应关系"


def _match_dictionary_value(
    source_value: Any, items: Sequence[Mapping[str, Any]]
) -> tuple[int | None, str | None, float, str]:
    scored: list[tuple[float, int, str, str]] = []
    for item in items:
        item_id = _as_int(item.get("id") or item.get("value_id"))
        item_value = item.get("value") or item.get("name")
        if item_id is None or item_value is None:
            continue
        score, reason = _dictionary_score(source_value, item_value)
        if score:
            scored.append((score, item_id, str(item_value), reason))
    scored.sort(key=lambda value: (-value[0], value[1]))
    if not scored or scored[0][0] < 0.80:
        return None, None, 0.0, "未找到置信度不低于 0.80 的合法字典值"
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, None, scored[0][0], "多个字典值得分相同，无法唯一确定"
    best = scored[0]
    return best[1], best[2], best[0], best[3]


def _is_required(attribute: Mapping[str, Any]) -> bool:
    return bool(attribute.get("is_required") or attribute.get("required"))


def _is_dictionary(attribute: Mapping[str, Any]) -> bool:
    explicit = attribute.get("is_dictionary")
    if explicit is not None:
        return bool(explicit)
    dictionary_id = _as_int(attribute.get("dictionary_id"))
    return dictionary_id not in (None, 0)


def _map_attributes(
    provider: MetadataProvider,
    source: Mapping[str, Any],
    category_id: int,
    type_id: int,
    response: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    mapped: list[dict[str, Any]] = []
    confirmations: list[dict[str, Any]] = []
    required_total = 0
    required_mapped = 0

    attributes = _result_list(response)
    attributes.sort(key=lambda item: _as_int(item.get("id") or item.get("attribute_id")) or 0)
    for attribute in attributes:
        attribute_id = _as_int(attribute.get("id") or attribute.get("attribute_id"))
        name_ru = str(attribute.get("name") or attribute.get("attribute_name") or "")
        if attribute_id is None or not name_ru:
            continue
        required = _is_required(attribute)
        dictionary = _is_dictionary(attribute)
        if required:
            required_total += 1
        concept = _attribute_concept(name_ru)
        values: list[Any] = []
        source_refs: list[dict[str, Any]] = []
        if concept is not None:
            values, source_refs = _source_attribute_values(source, concept)

        item: dict[str, Any] = {
            "attribute_id": attribute_id,
            "attribute_name_ru": name_ru,
            "value": values[0] if len(values) == 1 else (values or None),
            "dictionary_value_id": None,
            "is_required": required,
            "is_dictionary": dictionary,
            "attribute_type": attribute.get("type"),
            "mapping_status": "NOT_MAPPED_OPTIONAL",
            "mapping_confidence": 0.0,
            "source_refs": source_refs,
        }

        if not values:
            if required:
                item["mapping_status"] = "NEEDS_CONFIRMATION"
                confirmations.append(
                    {
                        "field": "ozon_attribute",
                        "attribute_id": attribute_id,
                        "attribute_name_ru": name_ru,
                        "reason": "OZON 必填属性无法从 attributes_zh 或 sku_list 可靠映射",
                    }
                )
            mapped.append(item)
            continue

        if not dictionary:
            item["mapping_status"] = "MAPPED_DIRECT"
            item["mapping_confidence"] = 0.90
            if required:
                required_mapped += 1
            mapped.append(item)
            continue

        dictionary_response = provider.attribute_values(category_id, type_id, attribute_id)
        dictionary_items = _dictionary_items(dictionary_response)
        ids: list[int] = []
        dictionary_matches: list[dict[str, Any]] = []
        unmatched: list[Any] = []
        for source_value in values:
            value_id, legal_value, score, reason = _match_dictionary_value(
                source_value, dictionary_items
            )
            if value_id is None:
                unmatched.append(source_value)
            else:
                ids.append(value_id)
                dictionary_matches.append(
                    {
                        "source_value": source_value,
                        "dictionary_value": legal_value,
                        "dictionary_value_id": value_id,
                        "confidence": score,
                        "reason": reason,
                    }
                )
        item["dictionary_matches"] = dictionary_matches
        if unmatched:
            item["mapping_status"] = "NEEDS_CONFIRMATION"
            item["mapping_confidence"] = 0.0
            item["dictionary_value_id"] = None
            confirmations.append(
                {
                    "field": "ozon_dictionary_value",
                    "attribute_id": attribute_id,
                    "attribute_name_ru": name_ru,
                    "source_values": unmatched,
                    "reason": "至少一个 1688 值无法唯一匹配本次 API 返回的合法字典值",
                }
            )
        else:
            item["dictionary_value_id"] = ids[0] if len(ids) == 1 else ids
            item["mapping_status"] = "MAPPED_DICTIONARY"
            item["mapping_confidence"] = round(
                min(match["confidence"] for match in dictionary_matches), 3
            )
            if required:
                required_mapped += 1
        mapped.append(item)

    return mapped, confirmations, required_mapped, required_total


def map_listing(
    source: Mapping[str, Any],
    provider: MetadataProvider,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    preferred_category_type: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Build a pre-listing record using only IDs returned by the provider."""

    tree_response = provider.category_tree()
    candidates, preferred_matched = _rank_categories(
        tree_response, source, preferred_category_type
    )
    if not candidates:
        raise ValueError(
            "OZON 类目树没有可创建的末级 category/type；"
            + _category_tree_shape(tree_response)
        )
    selected = candidates[0]
    category_id = selected["ozon_category_id"]
    type_id = selected["ozon_type_id"]
    confirmations: list[dict[str, Any]] = []
    if preferred_category_type is not None and not preferred_matched:
        confirmations.append(
            {
                "field": "ozon_category_id",
                "reason": "AlphaShop 返回的 category/type 不存在于本次 OZON 可用末级类目树",
                "alphashop_candidate": {
                    "ozon_category_id": preferred_category_type[0],
                    "ozon_type_id": preferred_category_type[1],
                },
            }
        )
    if selected["confidence"] < confidence_threshold:
        confirmations.append(
            {
                "field": "ozon_category_id",
                "reason": "最高候选类目置信度低于自动选择阈值",
                "confidence": selected["confidence"],
                "threshold": confidence_threshold,
                "candidate_category_id": category_id,
                "candidate_type_id": type_id,
            }
        )

    attribute_response = provider.category_attributes(category_id, type_id)
    attributes, attribute_confirmations, required_mapped, required_total = _map_attributes(
        provider, source, category_id, type_id, attribute_response
    )
    confirmations.extend(attribute_confirmations)
    attribute_confidence = (
        required_mapped / required_total if required_total else 1.0
    )
    overall = 0.6 * selected["confidence"] + 0.4 * attribute_confidence
    source_category = source.get("category_id")
    if source_category is None and isinstance(source.get("category_path_zh"), Mapping):
        source_category = source["category_path_zh"].get("selected_category_id")
    return {
        "schema_version": "1.0",
        "source_offer_id": source.get("source_offer_id"),
        "source_category_id": source_category,
        "metadata_mode": provider.mode,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confidence_threshold": confidence_threshold,
        "ozon_category_id": category_id,
        "ozon_type_id": type_id,
        "mapping_confidence": {
            "category": selected["confidence"],
            "required_attributes": round(attribute_confidence, 3),
            "overall": round(overall, 3),
        },
        "category_selection_status": (
            "SELECTED" if selected["confidence"] >= confidence_threshold else "NEEDS_CONFIRMATION"
        ),
        "listing_readiness": "NEEDS_CONFIRMATION" if confirmations else "READY_FOR_NEXT_STAGE",
        "top_category_candidates": candidates,
        "alphashop_category_candidate_validated": preferred_matched,
        "ozon_attributes": attributes,
        "needs_confirmation": confirmations,
        "metadata_provenance": {"calls": provider.calls},
    }

