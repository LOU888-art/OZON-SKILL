"""Guarded upload state machine with persistent task tracking."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

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
    max_polls: int = 60,
    verification_max_polls: int = 12,
) -> dict[str, Any]:
    """Run one store independently; dry-run never calls the supplied client."""

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

    previous = upload_log.latest_for_payload(store.store_id, entry["payload_sha256"])
    log_index: int | None = None
    resume_verification = False
    final_details: list[dict[str, Any]] = []
    if previous:
        previous_status = str(previous.get("status") or "")
        if previous_status in {"FAILED", "VERIFIED"}:
            previous["reused_existing_result"] = True
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
                }
                final_details = list(entry.get("import_items") or [])
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
        duplicate = [
            offer_id
            for offer_id in assembly.offer_ids
            if upload_log.has_task_for_offer(store.store_id, offer_id)
        ]
        if duplicate:
            entry["status"] = "SKIPPED_EXISTING_TASK"
            entry["duplicate_offer_ids"] = duplicate
            entry["history"].append({"at": _now(), "status": entry["status"]})
            upload_log.append(entry)
            return entry

        import_response = client.product_import(assembly.payload)
        task_id = _task_id(import_response)
        if task_id is None:
            entry["status"] = "IMPORT_RESPONSE_MISSING_TASK_ID"
            entry["request_sent"] = True
            entry["history"].append({"at": _now(), "status": entry["status"]})
            upload_log.append(entry)
            return entry

        # Persist the task before any polling or follow-up request.
        entry["request_sent"] = True
        entry["task_id"] = task_id
        entry["status"] = "SUBMITTED"
        entry["history"].append(
            {"at": _now(), "status": "SUBMITTED", "task_id": task_id}
        )
        log_index = upload_log.append(entry)

    if not resume_verification:
        final_state = "PENDING"
        for poll_number in range(1, max_polls + 1):
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

    attribute_verification: dict[str, Any] = {}
    implicit_type_attributes = _top_level_type_attribute_ids(
        assembly.payload, assembly.required_attribute_ids
    )
    for verification_poll in range(1, max(1, verification_max_polls) + 1):
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

