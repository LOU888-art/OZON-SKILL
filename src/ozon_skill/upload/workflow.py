"""Guarded upload state machine with persistent task tracking."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .assembler import AssemblyResult
from .config import StoreConfig
from .log import UploadLog


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _task_id(response: Mapping[str, Any]) -> int | None:
    result = response.get("result")
    value = result.get("task_id") if isinstance(result, Mapping) else response.get("task_id")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_items(response: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = response.get("result")
    if isinstance(result, Mapping):
        items = result.get("items") or result.get("products") or []
    elif isinstance(result, list):
        items = result
    else:
        items = response.get("items") or []
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _rich_content_value(attribute: Mapping[str, Any]) -> dict[str, Any] | None:
    for value in attribute.get("values") or []:
        if not isinstance(value, Mapping) or not isinstance(value.get("value"), str):
            continue
        try:
            candidate = json.loads(value["value"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("version") == 0.3
            and isinstance(candidate.get("content"), list)
        ):
            return candidate
    return None


def _split_rich_content_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Keep external Rich Content URLs out of the initial product import."""

    cleaned = deepcopy(dict(payload))
    plans: dict[str, list[dict[str, Any]]] = {}
    for item in cleaned.get("items") or []:
        if not isinstance(item, dict):
            continue
        offer_id = str(item.get("offer_id") or "")
        kept: list[Any] = []
        for attribute in item.get("attributes") or []:
            if not isinstance(attribute, Mapping):
                kept.append(attribute)
                continue
            rich_value = _rich_content_value(attribute)
            if rich_value is None:
                kept.append(attribute)
                continue
            if offer_id:
                plans.setdefault(offer_id, []).append(
                    {
                        "complex_id": int(attribute.get("complex_id") or 0),
                        "id": attribute.get("id") or attribute.get("attribute_id"),
                        "value": rich_value,
                    }
                )
        item["attributes"] = kept
    return cleaned, plans


def _is_ozon_resource_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "ozon.ru"
        or host.endswith(".ozon.ru")
        or host.endswith(".ozone.ru")
        or host.endswith(".ozonusercontent.com")
        or host.endswith(".ozonusercontent.ru")
    )


def _ozon_image_urls(item: Mapping[str, Any]) -> list[str]:
    result: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and _is_ozon_resource_url(value):
            result.append(value)
        elif isinstance(value, Mapping):
            # Once inside an image field, Ozon may expose renditions under
            # version-specific names such as big/mini/default/original.
            for child in value.values():
                add(child)
        elif isinstance(value, list):
            for child in value:
                add(child)

    for key, value in item.items():
        normalized_key = str(key).lower()
        if any(marker in normalized_key for marker in ("image", "photo", "picture")):
            add(value)
    return list(dict.fromkeys(result))


def _ozon_image_urls_anywhere(item: Mapping[str, Any]) -> list[str]:
    """Find CDN image resources even when Ozon nests them under `sources`."""

    candidates = _ozon_image_urls({"images": item})
    result: list[str] = []
    for value in candidates:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        if (
            host.endswith(".ozone.ru")
            or host.endswith(".ozonusercontent.com")
            or host.endswith(".ozonusercontent.ru")
            or any(marker in host for marker in ("cdn", "image", "img", "media", "ir."))
            or any(marker in path for marker in ("/s3/", "multimedia", "/image/", "/images/"))
            or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        ):
            result.append(value)
    return list(dict.fromkeys(result))


def _hosted_rich_content_updates(
    plans: Mapping[str, Sequence[Mapping[str, Any]]],
    info_response: Mapping[str, Any],
    picture_urls_by_offer: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    by_offer = {
        str(item.get("offer_id") or ""): item for item in _result_items(info_response)
    }
    updates: list[dict[str, Any]] = []
    errors: list[str] = []
    for offer_id, attributes in plans.items():
        hosted_images = list((picture_urls_by_offer or {}).get(offer_id) or [])
        hosted_images.extend(_ozon_image_urls_anywhere(by_offer.get(offer_id, {})))
        hosted_images = list(dict.fromkeys(hosted_images))
        if not hosted_images:
            errors.append(f"offer_id={offer_id} 未取得 Ozon 托管图片 URL")
            continue
        rewritten_attributes: list[dict[str, Any]] = []
        for attribute in attributes:
            attribute_id = attribute.get("id")
            if not isinstance(attribute_id, int):
                errors.append(f"offer_id={offer_id} 的 Rich Content 属性 ID 无效")
                continue
            rich_value = deepcopy(attribute.get("value"))
            image_index = 0
            for component in rich_value.get("content") or []:
                if not isinstance(component, dict):
                    continue
                for block in component.get("blocks") or []:
                    if not isinstance(block, dict) or not isinstance(block.get("img"), dict):
                        continue
                    hosted_url = hosted_images[image_index % len(hosted_images)]
                    image_index += 1
                    block["img"]["src"] = hosted_url
                    block["img"]["srcMobile"] = hosted_url
                    block["imgLink"] = ""
            serialized = json.dumps(
                rich_value, ensure_ascii=False, separators=(",", ":")
            )
            if not hosted_images or any(
                not _is_ozon_resource_url(block.get("img", {}).get(field))
                for component in rich_value.get("content") or []
                if isinstance(component, Mapping)
                for block in component.get("blocks") or []
                if isinstance(block, Mapping) and isinstance(block.get("img"), Mapping)
                for field in ("src", "srcMobile")
            ):
                errors.append(f"offer_id={offer_id} 的 Rich Content 仍包含外部图片")
                continue
            rewritten_attributes.append(
                {
                    "complex_id": int(attribute.get("complex_id") or 0),
                    "id": attribute_id,
                    "values": [{"value": serialized}],
                }
            )
        if rewritten_attributes:
            updates.append(
                {"offer_id": offer_id, "attributes": rewritten_attributes}
            )
    return updates, errors


def _product_ids_by_offer(
    info_response: Mapping[str, Any],
    import_items: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in [*_result_items(info_response), *import_items]:
        offer_id = str(item.get("offer_id") or "")
        raw_product_id = item.get("id") or item.get("product_id")
        if not offer_id or isinstance(raw_product_id, bool):
            continue
        try:
            product_id = int(raw_product_id)
        except (TypeError, ValueError):
            continue
        if product_id > 0:
            result[offer_id] = product_id
    return result


def _picture_urls_by_offer(
    response: Mapping[str, Any], product_ids_by_offer: Mapping[str, int]
) -> dict[str, list[str]]:
    offer_by_product = {
        product_id: offer_id for offer_id, product_id in product_ids_by_offer.items()
    }
    result: dict[str, list[str]] = {}
    for item in _result_items(response):
        raw_product_id = item.get("product_id") or item.get("id")
        if isinstance(raw_product_id, bool):
            continue
        try:
            product_id = int(raw_product_id)
        except (TypeError, ValueError):
            continue
        offer_id = offer_by_product.get(product_id)
        if not offer_id:
            continue
        # This endpoint uses rendition keys such as primary/secondary/color,
        # so the entire item is known to be an image container.
        urls = _ozon_image_urls_anywhere(item)
        if urls:
            result[offer_id] = urls
    return result


def _picture_response_diagnostics(response: Mapping[str, Any]) -> dict[str, Any]:
    items = _result_items(response)
    return {
        "root_keys": sorted(map(str, response.keys()))[:20],
        "item_count": len(items),
        "items": [
            {
                "product_id": item.get("product_id") or item.get("id"),
                "status": item.get("status"),
                "errors": item.get("errors"),
                "keys": sorted(map(str, item.keys()))[:30],
                "ozon_image_count": len(_ozon_image_urls_anywhere(item)),
            }
            for item in items[:100]
        ],
    }


def _source_images_by_offer(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        offer_id = str(item.get("offer_id") or "")
        images = [
            value
            for value in item.get("images") or []
            if isinstance(value, str) and value.startswith("https://")
        ]
        primary = item.get("primary_image")
        if isinstance(primary, str) and primary.startswith("https://"):
            images.insert(0, primary)
        if offer_id and images:
            result[offer_id] = list(dict.fromkeys(images))[:15]
    return result


def _response_diagnostics(response: Mapping[str, Any]) -> dict[str, Any]:
    """Keep useful Ozon response metadata without copying a large payload."""

    result = response.get("result")
    return {
        "root_keys": sorted(map(str, response.keys()))[:30],
        "result_type": type(result).__name__,
        "result_keys": (
            sorted(map(str, result.keys()))[:30]
            if isinstance(result, Mapping)
            else []
        ),
        "task_id": _task_id(response),
        "code": response.get("code"),
        "message": response.get("message"),
    }


def _poll_state(response: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    items = _result_items(response)
    details: list[dict[str, Any]] = []
    if not items:
        return "PENDING", details
    pending = False
    failed = False
    for item in items:
        status = str(item.get("status") or "").lower()
        errors = item.get("errors") or []
        details.append(
            {
                "offer_id": item.get("offer_id"),
                "product_id": item.get("product_id"),
                "status": status or None,
                "errors": errors,
            }
        )
        if status in {"imported", "success", "succeeded", "completed"} and not errors:
            continue
        if status in {"failed", "error", "declined"} or errors:
            failed = True
        else:
            pending = True
    if failed:
        return "FAILED", details
    if pending:
        return "PENDING", details
    return "SUCCEEDED", details


_PLANT_NAME_ERROR = "FB_ERROR_SEEDS_IN_NAME"
_FALSE_POSITIVE_PLANT_TERMS_RU = re.compile(
    r"\bсухоцвет\w*\b",
    re.IGNORECASE,
)
_REGULATED_PLANT_TERMS_RU = re.compile(
    r"\b(?:семен\w*|саженц\w*|рассад\w*|луковиц\w*|клубн\w*|"
    r"корневищ\w*|жив\w*\s+растени\w*)\b",
    re.IGNORECASE,
)
_FLOWER_TERMS_RU = re.compile(
    r"\b(?:цвет\w*|гардени\w*|орхиде\w*|фрези\w*|колокольчик\w*|"
    r"роз\w*|лаванд\w*|жасмин\w*|лили\w*|пион\w*)\b",
    re.IGNORECASE,
)


def _clean_offer_name(value: Any, *, aggressive: bool = False, offer_id: str = "") -> str:
    """Remove botanical terms that Ozon can misclassify as prohibited plants."""

    name = str(value or "").strip()
    name = _FALSE_POSITIVE_PLANT_TERMS_RU.sub("", name)
    if aggressive:
        name = _FLOWER_TERMS_RU.sub("", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"\s*[,;:/|]+\s*", ", ", name)
    name = re.sub(r"(?:,\s*){2,}", ", ", name).strip(" ,;:—-")
    suffix = str(offer_id or "")[-4:]
    addition = f"вариант товара {suffix}" if suffix else "вариант товара"
    if aggressive and addition.casefold() not in name.casefold():
        name = f"{name}, {addition}".strip(" ,")
    if len(name) < 60:
        name = f"{name} — декоративный товар для использования по назначению".strip()
    if len(name) > 200:
        name = name[:200].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return name


def _looks_like_regulated_plant(value: Any) -> bool:
    """Do not rewrite genuine seeds/live plants to evade Ozon restrictions."""

    name = str(value or "")
    if re.search(r"\bискусствен\w*\b", name, re.IGNORECASE):
        return False
    return bool(_REGULATED_PLANT_TERMS_RU.search(name))


def _sanitize_import_names(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    cleaned = deepcopy(dict(payload))
    changed: list[str] = []
    for item in cleaned.get("items") or []:
        if not isinstance(item, dict):
            continue
        offer_id = str(item.get("offer_id") or "")
        if not _FALSE_POSITIVE_PLANT_TERMS_RU.search(str(item.get("name") or "")):
            continue
        safe_name = _clean_offer_name(item.get("name"), offer_id=offer_id)
        if safe_name and safe_name != item.get("name"):
            item["name"] = safe_name
            changed.append(offer_id)
    return cleaned, changed


def _error_codes(item: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    errors = item.get("errors") or []
    if not isinstance(errors, list):
        errors = [errors]
    for error in errors:
        if isinstance(error, Mapping):
            code = error.get("code") or error.get("error_code")
            if code:
                result.add(str(code).upper())
        elif error and _PLANT_NAME_ERROR in str(error).upper():
            result.add(_PLANT_NAME_ERROR)
    return result


def _plant_name_rejected_offers(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(item.get("offer_id") or "")
        for item in items
        if _PLANT_NAME_ERROR in _error_codes(item) and item.get("offer_id")
    }


def _merge_import_items(
    original: Sequence[Mapping[str, Any]], repaired: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_offer = {
        str(item.get("offer_id") or ""): dict(item)
        for item in original
        if item.get("offer_id")
    }
    for item in repaired:
        offer_id = str(item.get("offer_id") or "")
        if offer_id:
            by_offer[offer_id] = dict(item)
    return list(by_offer.values())


def _archived_product_ids(response: Mapping[str, Any]) -> list[int]:
    result: list[int] = []
    for item in _result_items(response):
        statuses = item.get("statuses") if isinstance(item.get("statuses"), Mapping) else {}
        archived = item.get("is_archived") or statuses.get("is_archived")
        product_id = item.get("id") or item.get("product_id")
        if archived and isinstance(product_id, int):
            result.append(product_id)
    return result


def _verify_required_attributes(
    response: Mapping[str, Any],
    offer_ids: Sequence[str],
    required_ids: Sequence[int],
    implicitly_present_by_offer: Mapping[str, set[int]] | None = None,
) -> dict[str, Any]:
    def attribute_ids(value: Any) -> set[int]:
        found: set[int] = set()
        if isinstance(value, list):
            for child in value:
                found.update(attribute_ids(child))
        elif isinstance(value, Mapping):
            raw_id = value.get("id") or value.get("attribute_id")
            if not isinstance(raw_id, bool):
                try:
                    found.add(int(raw_id))
                except (TypeError, ValueError):
                    pass
            for key in ("attributes", "complex_attributes"):
                found.update(attribute_ids(value.get(key)))
        return found

    expected = set(required_ids)
    found_by_offer: dict[str, set[int]] = {}
    for item in _result_items(response):
        offer_id = str(item.get("offer_id") or "")
        found_by_offer[offer_id] = attribute_ids(
            {
                "attributes": item.get("attributes") or [],
                "complex_attributes": item.get("complex_attributes") or [],
            }
        )
    implicit = implicitly_present_by_offer or {}
    missing = {
        offer_id: sorted(
            expected
            - found_by_offer.get(offer_id, set())
            - implicit.get(offer_id, set())
        )
        for offer_id in offer_ids
        if (
            expected
            - found_by_offer.get(offer_id, set())
            - implicit.get(offer_id, set())
        )
    }
    return {
        "required_attribute_ids": sorted(expected),
        "returned_offer_ids": sorted(found_by_offer),
        "found_attribute_ids_by_offer": {
            offer_id: sorted(found) for offer_id, found in found_by_offer.items()
        },
        "implicitly_present_attribute_ids_by_offer": {
            offer_id: sorted(found) for offer_id, found in implicit.items() if found
        },
        "missing_by_offer_id": missing,
        "all_required_attributes_present": not missing,
    }


def _top_level_type_attribute_ids(
    payload: Mapping[str, Any], required_ids: Sequence[int]
) -> dict[str, set[int]]:
    """Find type-selector attributes represented by Ozon's top-level type_id.

    Ozon accepts these attributes during import but does not echo them in the
    v4 attributes collection because the same selection is returned as type_id.
    """

    required = set(required_ids)
    result: dict[str, set[int]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        offer_id = str(item.get("offer_id") or "")
        type_id = item.get("type_id")
        if not offer_id or not isinstance(type_id, int):
            continue
        matched: set[int] = set()
        for attribute in item.get("attributes") or []:
            if not isinstance(attribute, Mapping):
                continue
            attribute_id = attribute.get("id") or attribute.get("attribute_id")
            if not isinstance(attribute_id, int) or attribute_id not in required:
                continue
            if any(
                isinstance(value, Mapping)
                and value.get("dictionary_value_id") == type_id
                for value in attribute.get("values") or []
            ):
                matched.add(attribute_id)
        if matched:
            result[offer_id] = matched
    return result


def run_upload_workflow(
    *,
    store: StoreConfig,
    assembly: AssemblyResult,
    upload_log: UploadLog,
    execute: bool,
    client: Any | None = None,
    supplemental_attributes: list[dict[str, Any]] | None = None,
    poll_interval_seconds: float = 5.0,
    max_polls: int = 36,
    verification_max_polls: int = 8,
    image_hosting_max_polls: int = 12,
    deadline_seconds: float = 420.0,
    fallback_source_images_by_offer: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Run one store independently; dry-run never calls the supplied client."""

    # Original supplier media is evidence only. Never send 1688 images to
    # Ozon as a recovery path when the required AI gallery is unavailable.
    fallback_source_images_by_offer = None

    started_at = time.monotonic()
    run_id = str(uuid.uuid4())
    entry: dict[str, Any] = {
        "run_id": run_id,
        "created_at": _now(),
        "store_id": store.store_id,
        "mode": "execute" if execute else "dry_run",
        "request_sent": False,
        "offer_ids": assembly.offer_ids,
        "payload_sha256": _payload_hash(assembly.payload),
        "dimension_source": assembly.dimension_source,
        "warnings": assembly.warnings,
        "validation_errors": assembly.validation_errors,
        "task_id": None,
        "status": "CREATED",
        "history": [],
    }
    import_payload, rich_content_plans = _split_rich_content_payload(assembly.payload)
    import_payload, preflight_name_repairs = _sanitize_import_names(import_payload)
    if preflight_name_repairs:
        entry["preflight_name_repairs"] = preflight_name_repairs
        entry["warnings"] = list(entry.get("warnings") or []) + [
            "为避免 Ozon 将商品误判为活体植物/种子，已清理标题中的高风险植物词"
        ]
    if rich_content_plans:
        entry["rich_content_strategy"] = "OZON_HOSTED_IMAGES_AFTER_IMPORT"

    if not execute:
        entry["status"] = (
            "DRY_RUN_BLOCKED" if assembly.validation_errors else "DRY_RUN_READY"
        )
        entry["history"].append(
            {"at": _now(), "status": entry["status"], "request_sent": False}
        )
        upload_log.append(entry)
        return entry

    if assembly.validation_errors:
        entry["status"] = "EXECUTION_BLOCKED"
        entry["history"].append({"at": _now(), "status": "EXECUTION_BLOCKED"})
        upload_log.append(entry)
        return entry
    if client is None:
        raise ValueError("an OZON client is required in execute mode")

    def deadline_exceeded(phase: str) -> bool:
        if deadline_seconds <= 0 or time.monotonic() - started_at < deadline_seconds:
            return False
        error = (
            f"Ozon 第五阶段超过 {int(deadline_seconds)} 秒安全时限，已停止等待；"
            f"当前环节：{phase}。已取得的 task_id 会保留，下次运行将继续回查，"
            "不会重复创建商品"
        )
        entry.setdefault("history", []).append(
            {"at": _now(), "status": "WORKFLOW_TIMEOUT", "phase": phase, "error": error}
        )
        if log_index is not None:
            upload_log.update(
                log_index,
                status="WORKFLOW_TIMEOUT",
                error=error,
                history=entry["history"],
            )
        return True

    previous = upload_log.latest_for_payload(store.store_id, entry["payload_sha256"])
    log_index: int | None = None
    resume_verification = False
    resume_name_repair = False
    final_details: list[dict[str, Any]] = []
    if previous and str(previous.get("status") or "") == "FAILED":
        rejected = _plant_name_rejected_offers(
            [
                item
                for item in previous.get("import_items") or []
                if isinstance(item, Mapping)
            ]
        )
        if rejected and isinstance(previous.get("task_id"), int):
            entry["retry_of_run_id"] = previous.get("run_id")
            entry["retry_reason"] = _PLANT_NAME_ERROR
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "RETRYING_REJECTED_NAMES",
                    "offer_ids": sorted(rejected),
                }
            )
            previous = None
        else:
            previous["reused_existing_result"] = True
            return previous
    if previous:
        previous_status = str(previous.get("status") or "")
        previous_rich_content_deferred = bool(
            previous.get("rich_content_deferred")
        )
        if previous_status == "VERIFIED" and not previous_rich_content_deferred:
            previous["reused_existing_result"] = True
            return previous
        repair_outcome_unknown = previous_status in {
            "NAME_REPAIR_SUBMITTING",
            "NAME_REPAIR_OUTCOME_UNKNOWN",
            "NAME_REPAIR_MISSING_TASK_ID",
        }
        initial_outcome_unknown = previous_status in {
            "SUBMITTING",
            "SUBMISSION_OUTCOME_UNKNOWN",
            "IMPORT_RESPONSE_MISSING_TASK_ID",
        } and not isinstance(previous.get("task_id"), int)
        if repair_outcome_unknown or initial_outcome_unknown:
            previous["reused_existing_result"] = True
            previous["manual_reconciliation_required"] = True
            previous.setdefault(
                "error",
                "上一次请求可能已经到达 Ozon，但没有取得 task_id；"
                "为防止重复创建，系统已停止自动重提，请先在 Ozon 后台按 offer_id 核对",
            )
            return previous
        previous_run_id = previous.get("run_id")
        log_index = next(
            (
                index
                for index, saved in enumerate(upload_log.data["entries"])
                if isinstance(saved, Mapping) and saved.get("run_id") == previous_run_id
            ),
            None,
        )
        if log_index is not None:
            entry = upload_log.data["entries"][log_index]
            task_id = entry.get("task_id")
            if not isinstance(task_id, int):
                log_index = None
            else:
                resume_verification = previous_status in {
                    "INFO_LIST_ERROR",
                    "UNARCHIVE_ERROR",
                    "ATTRIBUTE_VERIFY_ERROR",
                    "VERIFICATION_FAILED",
                    "RICH_CONTENT_IMAGES_PENDING",
                    "RICH_CONTENT_IMAGES_MISSING",
                    "RICH_CONTENT_UPDATE_ERROR",
                } or (
                    previous_status == "VERIFIED"
                    and previous_rich_content_deferred
                )
                final_details = list(entry.get("import_items") or [])
                resume_name_repair = bool(entry.get("name_repair_attempted"))
                entry.setdefault("history", []).append(
                    {
                        "at": _now(),
                        "status": (
                            "RESUMING_VERIFICATION"
                            if resume_verification
                            else "RESUMING_TASK_POLL"
                        ),
                        "task_id": task_id,
                    }
                )
                upload_log.update(
                    log_index,
                    status=(
                        "RESUMING_VERIFICATION"
                        if resume_verification
                        else "RESUMING_TASK_POLL"
                    ),
                    error=None,
                    history=entry["history"],
                )

    if log_index is None:
        uncertain = [
            offer_id
            for offer_id in assembly.offer_ids
            if upload_log.has_uncertain_submission_for_offer(
                store.store_id, offer_id
            )
        ]
        if uncertain:
            entry["status"] = "SKIPPED_UNCERTAIN_SUBMISSION"
            entry["duplicate_offer_ids"] = uncertain
            entry["history"].append({"at": _now(), "status": entry["status"]})
            upload_log.append(entry)
            return entry

        # Persist the intent before touching Ozon. If the process exits or the
        # response is lost after the request starts, the next run must not
        # blindly create the same offers again.
        entry["request_sent"] = True
        entry["status"] = "SUBMITTING"
        entry["history"].append({"at": _now(), "status": "SUBMITTING"})
        log_index = upload_log.append(entry)
        try:
            import_response = client.product_import(import_payload)
        except Exception as exc:
            error = (
                "Ozon 提交结果不确定：请求过程中连接中断。为防止重复创建，"
                "系统不会自动重提；请先在 Ozon 后台按 offer_id 核对。"
                f"原始错误：{str(exc)[:500]}"
            )
            entry["history"].append(
                {"at": _now(), "status": "SUBMISSION_OUTCOME_UNKNOWN", "error": error}
            )
            upload_log.update(
                log_index,
                status="SUBMISSION_OUTCOME_UNKNOWN",
                error=error,
                history=entry["history"],
            )
            return upload_log.data["entries"][log_index]
        task_id = _task_id(import_response) if isinstance(import_response, Mapping) else None
        if task_id is None:
            error = (
                "Ozon 已响应但没有返回 task_id。为防止重复创建，系统不会自动重提；"
                "请先在 Ozon 后台按 offer_id 核对"
            )
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "IMPORT_RESPONSE_MISSING_TASK_ID",
                    "error": error,
                }
            )
            upload_log.update(
                log_index,
                status="IMPORT_RESPONSE_MISSING_TASK_ID",
                error=error,
                history=entry["history"],
            )
            return upload_log.data["entries"][log_index]

        # Persist the task before any polling or follow-up request.
        entry["task_id"] = task_id
        entry["status"] = "SUBMITTED"
        entry["history"].append(
            {"at": _now(), "status": "SUBMITTED", "task_id": task_id}
        )
        upload_log.update(
            log_index,
            task_id=task_id,
            status="SUBMITTED",
            error=None,
            history=entry["history"],
        )

    if not resume_verification:
        final_state = "PENDING"
        for poll_number in range(1, max_polls + 1):
            if deadline_exceeded("商品导入任务回查"):
                return upload_log.data["entries"][log_index]
            try:
                response = client.import_info(task_id)
            except Exception as exc:
                entry["history"].append(
                    {"at": _now(), "status": "POLL_API_ERROR", "error": str(exc)[:500]}
                )
                upload_log.update(
                    log_index,
                    status="POLL_API_ERROR",
                    error=str(exc)[:500],
                    history=entry["history"],
                )
                return upload_log.data["entries"][log_index]
            final_state, final_details = _poll_state(response)
            if resume_name_repair:
                final_details = _merge_import_items(
                    [
                        item
                        for item in entry.get("initial_import_items") or []
                        if isinstance(item, Mapping)
                    ],
                    final_details,
                )
            entry["history"].append(
                {
                    "at": _now(),
                    "status": final_state,
                    "poll_number": poll_number,
                    "items": final_details,
                }
            )
            upload_log.update(
                log_index,
                status=final_state,
                error=None,
                history=entry["history"],
                import_items=final_details,
            )
            if final_state != "PENDING":
                break
            if poll_interval_seconds:
                time.sleep(poll_interval_seconds)
        else:
            final_state = "POLL_TIMEOUT"
            entry["history"].append({"at": _now(), "status": final_state})
            upload_log.update(log_index, status=final_state, history=entry["history"])
            return upload_log.data["entries"][log_index]

        if final_state == "FAILED":
            failed_items = [
                item
                for item in final_details
                if str(item.get("status") or "").lower()
                in {"failed", "error", "declined"}
                or item.get("errors")
            ]
            rejected_offers = _plant_name_rejected_offers(failed_items)
            repairable = bool(failed_items) and all(
                _PLANT_NAME_ERROR in _error_codes(item) for item in failed_items
            )
            source_items = {
                str(item.get("offer_id") or ""): item
                for item in import_payload.get("items") or []
                if isinstance(item, Mapping) and item.get("offer_id")
            }
            repair_items: list[dict[str, Any]] = []
            if repairable and not entry.get("name_repair_attempted"):
                for offer_id in sorted(rejected_offers):
                    source = source_items.get(offer_id)
                    if source is None or _looks_like_regulated_plant(
                        source.get("name")
                    ):
                        repairable = False
                        break
                    repaired = deepcopy(dict(source))
                    repaired["name"] = _clean_offer_name(
                        repaired.get("name"), aggressive=True, offer_id=offer_id
                    )
                    repair_items.append(repaired)

            if repairable and repair_items:
                initial_task_id = task_id
                initial_details = list(final_details)
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "NAME_REPAIR_SUBMITTING",
                        "offer_ids": sorted(rejected_offers),
                    }
                )
                upload_log.update(
                    log_index,
                    status="NAME_REPAIR_SUBMITTING",
                    error=None,
                    name_repair_offer_ids=sorted(rejected_offers),
                    name_repair_attempted=True,
                    initial_task_id=initial_task_id,
                    initial_import_items=initial_details,
                    history=entry["history"],
                )
                try:
                    repair_response = client.product_import({"items": repair_items})
                except Exception as exc:
                    error = (
                        "Ozon 标题修复请求的结果不确定，系统不会自动重复提交；"
                        "请先按 offer_id 在 Ozon 后台核对。"
                        f"原始错误：{str(exc)[:500]}"
                    )
                    entry["history"].append(
                        {
                            "at": _now(),
                            "status": "NAME_REPAIR_OUTCOME_UNKNOWN",
                            "error": error,
                        }
                    )
                    upload_log.update(
                        log_index,
                        status="NAME_REPAIR_OUTCOME_UNKNOWN",
                        error=error,
                        history=entry["history"],
                    )
                    return upload_log.data["entries"][log_index]

                repair_task_id = (
                    _task_id(repair_response)
                    if isinstance(repair_response, Mapping)
                    else None
                )
                if repair_task_id is None:
                    error = (
                        "Ozon 已接收标题修复请求但没有返回 task_id；"
                        "系统不会自动重复提交，请先在 Ozon 后台核对"
                    )
                    entry["history"].append(
                        {
                            "at": _now(),
                            "status": "NAME_REPAIR_MISSING_TASK_ID",
                            "error": error,
                        }
                    )
                    upload_log.update(
                        log_index,
                        status="NAME_REPAIR_MISSING_TASK_ID",
                        error=error,
                        history=entry["history"],
                    )
                    return upload_log.data["entries"][log_index]

                task_id = repair_task_id
                entry["task_id"] = task_id
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "NAME_REPAIR_SUBMITTED",
                        "task_id": task_id,
                        "offer_ids": sorted(rejected_offers),
                    }
                )
                upload_log.update(
                    log_index,
                    task_id=task_id,
                    status="NAME_REPAIR_SUBMITTED",
                    error=None,
                    repaired_names={
                        str(item.get("offer_id")): str(item.get("name"))
                        for item in repair_items
                    },
                    history=entry["history"],
                )

                repaired_details: list[dict[str, Any]] = []
                repair_state = "PENDING"
                for repair_poll in range(1, max_polls + 1):
                    if deadline_exceeded("被拒 SKU 的安全标题修复回查"):
                        return upload_log.data["entries"][log_index]
                    try:
                        repair_info = client.import_info(task_id)
                    except Exception as exc:
                        entry["history"].append(
                            {
                                "at": _now(),
                                "status": "NAME_REPAIR_POLL_API_ERROR",
                                "error": str(exc)[:500],
                            }
                        )
                        upload_log.update(
                            log_index,
                            status="NAME_REPAIR_POLL_API_ERROR",
                            error=str(exc)[:500],
                            history=entry["history"],
                        )
                        return upload_log.data["entries"][log_index]
                    repair_state, repaired_details = _poll_state(repair_info)
                    merged_details = _merge_import_items(
                        initial_details, repaired_details
                    )
                    entry["history"].append(
                        {
                            "at": _now(),
                            "status": f"NAME_REPAIR_{repair_state}",
                            "poll_number": repair_poll,
                            "items": repaired_details,
                        }
                    )
                    upload_log.update(
                        log_index,
                        status=(
                            "SUCCEEDED"
                            if repair_state == "SUCCEEDED"
                            else f"NAME_REPAIR_{repair_state}"
                        ),
                        error=None,
                        import_items=merged_details,
                        history=entry["history"],
                    )
                    if repair_state != "PENDING":
                        break
                    if poll_interval_seconds:
                        time.sleep(poll_interval_seconds)
                final_state = repair_state
                final_details = _merge_import_items(
                    initial_details, repaired_details
                )

        if final_state != "SUCCEEDED":
            return upload_log.data["entries"][log_index]

    if supplemental_attributes and not resume_verification:
        try:
            client.attributes_update(supplemental_attributes)
        except Exception as exc:
            entry["history"].append(
                {"at": _now(), "status": "ATTRIBUTE_UPDATE_ERROR", "error": str(exc)[:500]}
            )
            upload_log.update(
                log_index,
                status="ATTRIBUTE_UPDATE_ERROR",
                error=str(exc)[:500],
                history=entry["history"],
            )
            return upload_log.data["entries"][log_index]
        entry["history"].append({"at": _now(), "status": "ATTRIBUTES_UPDATED"})

    try:
        info_response = client.product_info_list(assembly.offer_ids)
    except Exception as exc:
        entry["history"].append(
            {"at": _now(), "status": "INFO_LIST_ERROR", "error": str(exc)[:500]}
        )
        upload_log.update(
            log_index,
            status="INFO_LIST_ERROR",
            error=str(exc)[:500],
            history=entry["history"],
        )
        return upload_log.data["entries"][log_index]
    archived_ids = _archived_product_ids(info_response)
    if archived_ids:
        try:
            client.unarchive(archived_ids)
        except Exception as exc:
            entry["history"].append(
                {"at": _now(), "status": "UNARCHIVE_ERROR", "error": str(exc)[:500]}
            )
            upload_log.update(
                log_index,
                status="UNARCHIVE_ERROR",
                error=str(exc)[:500],
                history=entry["history"],
            )
            return upload_log.data["entries"][log_index]
        entry["history"].append(
            {"at": _now(), "status": "UNARCHIVED", "product_ids": archived_ids}
        )
        try:
            info_response = client.product_info_list(assembly.offer_ids)
        except Exception as exc:
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "INFO_LIST_ERROR",
                    "error": str(exc)[:500],
                }
            )
            upload_log.update(
                log_index,
                status="INFO_LIST_ERROR",
                error=str(exc)[:500],
                history=entry["history"],
            )
            return upload_log.data["entries"][log_index]

    if rich_content_plans:
        rich_updates: list[dict[str, Any]] = []
        rich_errors: list[str] = []
        picture_urls: dict[str, list[str]] = {}
        picture_info_error: str | None = None
        picture_diagnostics: dict[str, Any] = {}
        product_ids_by_offer = _product_ids_by_offer(info_response, final_details)
        source_images_by_offer = _source_images_by_offer(assembly.payload)
        picture_api_is_v2 = entry.get("picture_import_api_version") == "v2-full"
        # Re-submitting pictures is idempotent and is safe on a later stage-5
        # retry.  A previous Ozon image task may have timed out even though the
        # product card itself was successfully created.
        picture_import_attempted = False
        fallback_import_attempted = bool(
            entry.get("picture_fallback_import_requested") and picture_api_is_v2
        )
        picture_import_errors: list[str] = []
        picture_import_responses: dict[str, Any] = {}
        fallback_import_errors: list[str] = []
        fallback_import_responses: dict[str, Any] = {}
        picture_task_ids: dict[str, int] = {
            str(offer_id): int(task_id)
            for offer_id, task_id in (
                entry.get("picture_import_task_ids") or {}
            ).items()
            if isinstance(task_id, int) and task_id > 0
        }
        picture_task_diagnostics: dict[str, Any] = {}
        image_poll_limit = max(1, image_hosting_max_polls)
        for image_poll in range(1, image_poll_limit + 1):
            if deadline_exceeded("Ozon 图片托管与富内容准备"):
                return upload_log.data["entries"][log_index]
            picture_task_diagnostics = {}
            for offer_id, picture_task_id in picture_task_ids.items():
                try:
                    task_response = client.import_info(picture_task_id)
                    task_state, task_items = _poll_state(task_response)
                    picture_task_diagnostics[offer_id] = {
                        "task_id": picture_task_id,
                        "state": task_state,
                        "items": task_items,
                    }
                except Exception as exc:
                    picture_task_diagnostics[offer_id] = {
                        "task_id": picture_task_id,
                        "state": "API_ERROR",
                        "error": str(exc)[:500],
                    }
            if product_ids_by_offer and hasattr(client, "product_pictures_info"):
                try:
                    pictures_response = client.product_pictures_info(
                        list(dict.fromkeys(product_ids_by_offer.values()))
                    )
                    picture_urls = _picture_urls_by_offer(
                        pictures_response, product_ids_by_offer
                    )
                    picture_diagnostics = _picture_response_diagnostics(
                        pictures_response
                    )
                    picture_info_error = None
                except Exception as exc:
                    picture_info_error = str(exc)[:500]
            rich_updates, rich_errors = _hosted_rich_content_updates(
                rich_content_plans, info_response, picture_urls
            )
            if not rich_errors and len(rich_updates) == len(rich_content_plans):
                break
            if (
                not picture_import_attempted
                and product_ids_by_offer
                and hasattr(client, "product_pictures_import")
            ):
                picture_import_attempted = True
                ready_offer_ids = {
                    str(item.get("offer_id") or "") for item in rich_updates
                }
                for offer_id in product_ids_by_offer:
                    if offer_id in ready_offer_ids:
                        continue
                    source_images = source_images_by_offer.get(offer_id) or []
                    if not source_images:
                        picture_import_errors.append(
                            f"offer_id={offer_id} 没有可重新提交的源图片"
                        )
                        continue
                    try:
                        import_response = client.product_pictures_import(
                            offer_id, source_images
                        )
                        picture_import_responses[offer_id] = _response_diagnostics(
                            import_response
                        )
                        picture_task_id = _task_id(import_response)
                        if picture_task_id is not None:
                            picture_task_ids[offer_id] = picture_task_id
                    except Exception as exc:
                        picture_import_errors.append(
                            f"offer_id={offer_id}: {str(exc)[:500]}"
                        )
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "PICTURES_IMPORT_REQUESTED",
                        "offer_count": len(product_ids_by_offer),
                        "errors": picture_import_errors,
                        "responses": picture_import_responses,
                    }
                )
                upload_log.update(
                    log_index,
                    picture_import_requested=True,
                    picture_import_api_version="v2-full",
                    picture_import_errors=picture_import_errors,
                    picture_import_responses=picture_import_responses,
                    picture_import_task_ids=picture_task_ids,
                    history=entry["history"],
                )
            # AI providers may return temporary URLs which browsers can open
            # but Ozon's fetcher cannot retrieve. After a short grace period,
            # seed the gallery with the corresponding original 1688 images.
            # Rich Content then uses Ozon's own hosted CDN renditions.
            fallback_at_poll = min(4, image_poll_limit)
            if (
                not fallback_import_attempted
                and image_poll >= fallback_at_poll
                and product_ids_by_offer
                and fallback_source_images_by_offer
                and hasattr(client, "product_pictures_import")
            ):
                fallback_import_attempted = True
                for offer_id in product_ids_by_offer:
                    fallback_images = [
                        value
                        for value in fallback_source_images_by_offer.get(offer_id, [])
                        if isinstance(value, str) and value.startswith("https://")
                    ]
                    fallback_images = list(dict.fromkeys(fallback_images))[:15]
                    if not fallback_images:
                        fallback_import_errors.append(
                            f"offer_id={offer_id} 没有对应的 1688 原始公共图片"
                        )
                        continue
                    try:
                        import_response = client.product_pictures_import(
                            offer_id, fallback_images
                        )
                        fallback_import_responses[offer_id] = _response_diagnostics(
                            import_response
                        )
                        picture_task_id = _task_id(import_response)
                        if picture_task_id is not None:
                            picture_task_ids[offer_id] = picture_task_id
                    except Exception as exc:
                        fallback_import_errors.append(
                            f"offer_id={offer_id}: {str(exc)[:500]}"
                        )
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "PICTURES_FALLBACK_IMPORT_REQUESTED",
                        "offer_count": len(product_ids_by_offer),
                        "errors": fallback_import_errors,
                        "responses": fallback_import_responses,
                    }
                )
                upload_log.update(
                    log_index,
                    picture_fallback_import_requested=True,
                    picture_import_api_version="v2-full",
                    picture_fallback_import_errors=fallback_import_errors,
                    picture_fallback_import_responses=fallback_import_responses,
                    picture_import_task_ids=picture_task_ids,
                    history=entry["history"],
                )
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "RICH_CONTENT_IMAGES_PENDING",
                    "poll_number": image_poll,
                    "offer_count": len(rich_content_plans),
                    "ready_offer_count": len(rich_updates),
                    "picture_offer_count": len(picture_urls),
                    "picture_info_error": picture_info_error,
                    "picture_diagnostics": picture_diagnostics,
                    "picture_import_errors": picture_import_errors,
                    "fallback_import_errors": fallback_import_errors,
                    "picture_task_diagnostics": picture_task_diagnostics,
                    "errors": rich_errors[:20],
                }
            )
            upload_log.update(
                log_index,
                status="RICH_CONTENT_IMAGES_PENDING",
                error=None,
                history=entry["history"],
            )
            if image_poll >= image_poll_limit:
                break
            if poll_interval_seconds:
                time.sleep(poll_interval_seconds)
            try:
                info_response = client.product_info_list(assembly.offer_ids)
                product_ids_by_offer.update(
                    _product_ids_by_offer(info_response, final_details)
                )
            except Exception as exc:
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "INFO_LIST_ERROR",
                        "error": str(exc)[:500],
                    }
                )
                upload_log.update(
                    log_index,
                    status="INFO_LIST_ERROR",
                    error=str(exc)[:500],
                    history=entry["history"],
                )
                return upload_log.data["entries"][log_index]
        rich_content_deferred = False
        if rich_errors or len(rich_updates) != len(rich_content_plans):
            error = (
                "; ".join(rich_errors)
                or "未生成完整的 Ozon 富内容更新"
            ) + f"；已等待并回查 {image_poll_limit} 次"
            if picture_info_error:
                error += f"；Ozon 图片信息接口：{picture_info_error}"
            elif picture_diagnostics:
                diagnostic_text = json.dumps(
                    picture_diagnostics, ensure_ascii=False, separators=(",", ":")
                )
                error += f"；图片接口诊断：{diagnostic_text[:1800]}"
            if picture_import_errors:
                error += f"；图片重传错误：{'; '.join(picture_import_errors)[:1200]}"
            if fallback_import_errors:
                error += f"；原图补传错误：{'; '.join(fallback_import_errors)[:1200]}"
            failed_picture_tasks = {
                offer_id: detail
                for offer_id, detail in picture_task_diagnostics.items()
                if detail.get("state") in {"FAILED", "API_ERROR"}
            }
            if failed_picture_tasks:
                task_text = json.dumps(
                    failed_picture_tasks,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                error += f"；图片任务诊断：{task_text[:1800]}"
            if rich_updates:
                try:
                    partial_rich_response = client.attributes_update(rich_updates)
                except Exception as exc:
                    error += f"；已就绪富内容写入失败：{str(exc)[:800]}"
                else:
                    entry["history"].append(
                        {
                            "at": _now(),
                            "status": "RICH_CONTENT_PARTIALLY_UPDATED",
                            "offer_count": len(rich_updates),
                        }
                    )
                    upload_log.update(
                        log_index,
                        rich_content_update=partial_rich_response,
                        rich_content_offer_count=len(rich_updates),
                        history=entry["history"],
                    )
            if picture_import_errors or fallback_import_errors or failed_picture_tasks:
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "RICH_CONTENT_IMAGE_WARNING",
                        "warning": error[:1600],
                    }
                )
            # The product import is already committed. Ozon can take much
            # longer to expose CDN renditions than it takes to publish the
            # card. Rich Content is optional and the Russian description is
            # already present, so a zero-image readback without an explicit
            # task error is a post-publish warning, not a failed listing.
            rich_content_deferred = True
            warning = (
                "商品已提交；Ozon 暂未返回托管图片，富内容已延后，"
                "商品简介继续生效。" + error
            )[:1600]
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "RICH_CONTENT_DEFERRED",
                    "warning": warning,
                }
            )
            upload_log.update(
                log_index,
                status="RICH_CONTENT_DEFERRED",
                error=None,
                rich_content_deferred=True,
                post_publish_warnings=[warning],
                history=entry["history"],
            )
        if not rich_content_deferred:
            try:
                rich_response = client.attributes_update(rich_updates)
            except Exception as exc:
                entry["history"].append(
                    {
                        "at": _now(),
                        "status": "RICH_CONTENT_UPDATE_ERROR",
                        "error": str(exc)[:1200],
                    }
                )
                upload_log.update(
                    log_index,
                    status="RICH_CONTENT_UPDATE_ERROR",
                    error=str(exc)[:1200],
                    history=entry["history"],
                )
                return upload_log.data["entries"][log_index]
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "RICH_CONTENT_UPDATED",
                    "offer_count": len(rich_updates),
                }
            )
            upload_log.update(
                log_index,
                rich_content_update=rich_response,
                rich_content_offer_count=len(rich_updates),
                rich_content_deferred=False,
                post_publish_warnings=[],
                history=entry["history"],
            )

    attribute_verification: dict[str, Any] = {}
    implicit_type_attributes = _top_level_type_attribute_ids(
        assembly.payload, assembly.required_attribute_ids
    )
    for verification_poll in range(1, max(1, verification_max_polls) + 1):
        if deadline_exceeded("商品属性回查验证"):
            return upload_log.data["entries"][log_index]
        try:
            attribute_response = client.product_info_attributes(assembly.offer_ids)
        except Exception as exc:
            entry["history"].append(
                {
                    "at": _now(),
                    "status": "ATTRIBUTE_VERIFY_ERROR",
                    "error": str(exc)[:500],
                }
            )
            upload_log.update(
                log_index,
                status="ATTRIBUTE_VERIFY_ERROR",
                error=str(exc)[:500],
                history=entry["history"],
            )
            return upload_log.data["entries"][log_index]
        attribute_verification = _verify_required_attributes(
            attribute_response,
            assembly.offer_ids,
            assembly.required_attribute_ids,
            implicit_type_attributes,
        )
        entry["history"].append(
            {
                "at": _now(),
                "status": "ATTRIBUTE_VERIFY_PENDING",
                "poll_number": verification_poll,
                "missing_by_offer_id": attribute_verification[
                    "missing_by_offer_id"
                ],
            }
        )
        if attribute_verification["all_required_attributes_present"]:
            break
        if verification_poll < max(1, verification_max_polls) and poll_interval_seconds:
            time.sleep(poll_interval_seconds)
    final_status = (
        "VERIFIED"
        if attribute_verification["all_required_attributes_present"]
        else "VERIFICATION_FAILED"
    )
    entry["history"].append({"at": _now(), "status": final_status})
    upload_log.update(
        log_index,
        status=final_status,
        error=None,
        history=entry["history"],
        import_items=final_details,
        product_info_list=info_response,
        attribute_verification=attribute_verification,
    )
    return upload_log.data["entries"][log_index]

