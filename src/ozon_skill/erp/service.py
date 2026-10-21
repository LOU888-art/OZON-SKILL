"""Five-stage pipeline orchestration and auditable gates for the web ERP."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from ozon_skill.ai_relay.client import AiRelayError
from ozon_skill.alphashop.client import (
    AlphaShopApiError,
    AlphaShopClient,
    AlphaShopHttpError,
    AlphaShopInsufficientPointsError,
    extract_external_category_pair,
    extract_offer_id,
)
from ozon_skill.alphashop.normalizer import normalize_alphashop_offer
from ozon_skill.category_mapping.mapper import CONFIDENCE_THRESHOLD, map_listing
from ozon_skill.category_mapping.provider import LiveMetadataProvider
from ozon_skill.content_generation.generator import build_final_listing
from ozon_skill.content_generation.media import (
    generate_all_images,
    process_image_plan,
    translate_all_images,
)
from ozon_skill.content_generation.rich_content import (
    BANNED_IMAGE_HOSTS,
    upgrade_legacy_rich_content,
    validate_public_image_url,
    validate_rich_content,
)
from ozon_skill.ozon.client import OzonApiError, OzonDescriptionCategoryClient
from ozon_skill.upload.assembler import AssemblyResult, assemble_import_payload
from ozon_skill.upload.client import OzonUploadClient
from ozon_skill.upload.config import load_store_configs
from ozon_skill.upload.log import UploadLog
from ozon_skill.upload.workflow import run_upload_workflow

from .ai_provider import AiProviderConfigurationError, AiProviderRegistry
from .stores import StoreAuthorizationError, StoreRegistry


STAGE_FILES = {
    1: "01_raw.json",
    2: "02_mapped.json",
    3: "03_final.json",
    4: "04_payload.json",
    5: "05_upload_log.json",
}
IMAGE_SELECTION_FILE = "image-selection.json"
IMAGE_ACTIONS = {"exclude", "translate", "reference_8", "sku_main"}
_PIPELINE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class PipelineError(RuntimeError):
    """Expected, user-facing pipeline failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PipelineError(f"缺少前置产物：{path.name}", status=409)
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"无法读取 {path.name}：{exc}", status=409) from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{path.name} 必须是 JSON 对象", status=409)
    return value


def _safe_pipeline_id(value: Any) -> str:
    result = str(value or "").strip()
    if not _PIPELINE_ID.fullmatch(result) or result in {".", ".."}:
        raise PipelineError("商品 ID 只能包含字母、数字、点、下划线和连字符")
    return result


def _required_env(*names: str) -> list[str]:
    return [name for name in names if not os.environ.get(name)]


def _source_for_mapper(raw: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(raw)
    attributes = raw.get("attributes_zh")
    if isinstance(attributes, Mapping):
        source["attributes_zh"] = [
            {"name": str(name), "value": value} for name, value in attributes.items()
        ]
    category_path = raw.get("category_path_zh")
    if isinstance(category_path, list):
        source["category_path_zh"] = {
            "selected_category_id": raw.get("category_id"),
            "path": category_path,
        }
    return source


def _image_items(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for key in ("images", "detail_images"):
        for item in source.get(key) or []:
            if isinstance(item, str):
                item = {"url": item}
            if isinstance(item, Mapping) and item.get("url") and str(item["url"]) not in seen:
                seen.add(str(item["url"]))
                result.append({**item, "source_role": key})
    for sku in source.get("sku_list") or []:
        if not isinstance(sku, Mapping) or not sku.get("image_url"):
            continue
        url = str(sku["image_url"])
        if url in seen:
            continue
        seen.add(url)
        result.append(
            {
                "url": url,
                "source_role": "sku_image",
                "sku_id": sku.get("sku_id"),
            }
        )
    return result


def gate_stage_1(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if not value.get("source_offer_id"):
        failures.append("source_offer_id 为空")
    if not value.get("images"):
        failures.append("images 至少需要 1 张主图")
    if not value.get("sku_list"):
        failures.append("sku_list 至少需要 1 个 SKU")
    if not value.get("category_id"):
        failures.append("category_id 为空")
    return failures


def _confirmation_attribute_ids(value: Mapping[str, Any]) -> set[int]:
    return {
        item["attribute_id"]
        for item in value.get("needs_confirmation") or []
        if isinstance(item, Mapping) and isinstance(item.get("attribute_id"), int)
    }


def gate_stage_2(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if not isinstance(value.get("ozon_type_id"), int):
        failures.append("ozon_type_id 为空")
    confirmations = _confirmation_attribute_ids(value)
    calls = value.get("metadata_provenance", {}).get("calls", [])
    for item in value.get("ozon_attributes") or []:
        if not isinstance(item, Mapping) or not item.get("is_dictionary"):
            continue
        attribute_id = item.get("attribute_id")
        dictionary_id = item.get("dictionary_value_id")
        if dictionary_id is None and attribute_id not in confirmations:
            failures.append(f"字典属性 {attribute_id} 既无合法 ID，也未进入人工确认")
        if dictionary_id is not None:
            traced = any(
                isinstance(call, Mapping)
                and call.get("endpoint") == "/v1/description-category/attribute/values"
                and call.get("request", {}).get("attribute_id") == attribute_id
                for call in calls
            )
            if not traced:
                failures.append(f"字典属性 {attribute_id} 的值 ID 无 API 调用追溯")
    return failures


def gate_stage_3(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    title = value.get("title_ru")
    if not isinstance(title, str) or not 150 <= len(title) <= 200:
        failures.append("title_ru 长度不在 150–200 字符")
    if isinstance(title, str) and (
        not re.search(r"[А-Яа-яЁё]", title)
        or re.search(r"[\u3400-\u9fff]", title)
    ):
        failures.append("title_ru 必须是自然俄语且不得包含中文")
    validation = value.get("title_validation")
    if not isinstance(validation, Mapping) or not validation.get("core_keyword_in_first_60"):
        failures.append("title_ru 前 60 字符未验证包含核心词")
    elif validation.get("status") != "VALID":
        failures.append("title_ru 未通过重复词根等标题规则")
    segments = value.get("title_segments")
    if not isinstance(segments, Mapping) or segments.get("brand") != "без бренда":
        failures.append("title_segments.brand 必须为无品牌")
    try:
        validate_rich_content(value.get("rich_content_json"))
    except ValueError as exc:
        failures.append(f"rich_content_json：{exc}")
    localized = value.get("localized_images")
    if not isinstance(localized, list) or not localized:
        failures.append("localized_images 为空")
    else:
        for index, item in enumerate(localized):
            url = item.get("final_public_url") if isinstance(item, Mapping) else None
            try:
                validate_public_image_url(str(url or ""))
            except ValueError as exc:
                detail = item.get("error") if isinstance(item, Mapping) else None
                failures.append(
                    f"localized_images[{index}]：{str(detail)[:240] if detail else exc}"
                )
    tags = value.get("tags_ru")
    if not isinstance(tags, list) or not tags or len(tags) > 30:
        failures.append("tags_ru 必须为 1–30 项的数组")
    elif any(
        not isinstance(tag, str) or re.search(r"[\u3400-\u9fff]", tag)
        for tag in tags
    ):
        failures.append("tags_ru 必须全部为自然俄语且不得包含中文")
    if not isinstance(value.get("description_ru"), str) or not value.get(
        "description_ru", ""
    ).strip():
        failures.append("description_ru 为空")
    elif re.search(r"[\u3400-\u9fff]", value["description_ru"]):
        failures.append("description_ru 不得包含中文")
    return failures


def gate_stage_4(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    items = value.get("items")
    if not isinstance(items, list) or not items:
        return ["items 必须是非空数组"]
    item = items[0] if isinstance(items[0], Mapping) else {}
    for field in ("offer_id", "type_id", "price", "attributes"):
        if item.get(field) in (None, "", []):
            failures.append(f"items[0].{field} 为空")
    return failures


def _missing_required_attributes(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for item in value.get("ozon_attributes") or []:
        if not isinstance(item, Mapping) or not item.get("is_required"):
            continue
        has_dictionary_value = any(
            isinstance(match, Mapping)
            and isinstance(match.get("dictionary_value_id"), int)
            for match in item.get("dictionary_matches") or []
        )
        has_direct_value = item.get("value") not in (None, "", [])
        is_missing = (
            not has_dictionary_value
            if item.get("is_dictionary")
            else not has_direct_value
        )
        if is_missing:
            missing.append(dict(item))
    return missing


def _ozon_dictionary_rows(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result")
    if isinstance(result, Mapping):
        result = result.get("values") or result.get("items") or []
    if not isinstance(result, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in result:
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("id") if item.get("id") is not None else item.get("value_id")
        label = item.get("value") if item.get("value") is not None else item.get("name")
        try:
            value_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if label is not None:
            rows.append({"dictionary_value_id": value_id, "value": str(label)})
    return rows


def _ozon_attribute_names(response: Mapping[str, Any]) -> dict[int, str]:
    result = response.get("result")
    if isinstance(result, Mapping):
        result = result.get("attributes") or result.get("items") or []
    if not isinstance(result, list):
        return {}
    names: dict[int, str] = {}
    for item in result:
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("id") if item.get("id") is not None else item.get(
            "attribute_id"
        )
        try:
            attribute_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = item.get("name") or item.get("attribute_name")
        if name:
            names[attribute_id] = str(name)
    return names


_ATTRIBUTE_NAME_ZH_FALLBACKS = (
    ("бренд", "品牌"),
    ("название цвета", "颜色名称"),
    ("название файла pdf", "PDF 文件名"),
    ("название", "商品名称"),
    ("аннотац", "商品简介"),
    ("вес с упаковкой", "含包装重量（克）"),
    ("вес товара", "商品重量（克）"),
    ("комплектац", "包装清单"),
    ("страна-изготовитель", "生产国家"),
    ("документ pdf", "PDF 文档"),
    ("единиц в одном товаре", "单件商品数量"),
    ("тип", "类型"),
    ("материал", "材质"),
    ("цвет", "颜色"),
    ("размер", "尺寸"),
    ("аромат", "香型"),
    ("запах", "气味"),
    ("описание", "商品描述"),
    ("тег", "主题标签"),
    ("контент", "富内容"),
)


def _attribute_name_zh_fallback(name_ru: Any, attribute_id: int) -> str:
    normalized = str(name_ru or "").lower().replace("ё", "е")
    for token, translated in _ATTRIBUTE_NAME_ZH_FALLBACKS:
        if token in normalized:
            return translated
    return f"Ozon 特征 ID {attribute_id}"


def _blocking_confirmations(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    required_ids = {
        item.get("attribute_id")
        for item in value.get("ozon_attributes") or []
        if isinstance(item, Mapping)
        and item.get("is_required")
        and isinstance(item.get("attribute_id"), int)
    }
    return [
        item
        for item in value.get("needs_confirmation") or []
        if isinstance(item, Mapping)
        and (
            item.get("attribute_id") in required_ids
            or item.get("field") == "ozon_category_id"
        )
    ]


def _upload_error_messages(value: Mapping[str, Any]) -> list[str]:
    messages: list[str] = []
    top_level_error = value.get("error")
    if top_level_error:
        messages.append(f"Ozon API 回查失败：{str(top_level_error)[:1200]}")
    verification = value.get("attribute_verification")
    if isinstance(verification, Mapping):
        missing_by_offer = verification.get("missing_by_offer_id")
        if isinstance(missing_by_offer, Mapping):
            for offer_id, missing_ids in missing_by_offer.items():
                if missing_ids:
                    rendered = ", ".join(str(item) for item in missing_ids)
                    messages.append(
                        f"Ozon 属性回读缺失：offer_id={offer_id}；属性 ID={rendered}"
                    )
                if len(messages) >= 20:
                    return messages
    for item in value.get("import_items") or []:
        if not isinstance(item, Mapping):
            continue
        offer_id = str(item.get("offer_id") or "未知 offer_id")
        errors = item.get("errors") or []
        if not isinstance(errors, list):
            errors = [errors]
        for error in errors:
            if isinstance(error, Mapping):
                code = error.get("code") or error.get("error_code") or "OZON_ERROR"
                description = (
                    error.get("description")
                    or error.get("message")
                    or error.get("text")
                    or error.get("details")
                    or "Ozon 未返回错误说明"
                )
                field = error.get("field") or error.get("attribute_id")
                suffix = f"；field={field}" if field not in (None, "") else ""
                messages.append(
                    f"Ozon 拒绝 {offer_id}：{code}；{str(description)[:500]}{suffix}"
                )
            elif error:
                messages.append(f"Ozon 拒绝 {offer_id}：{str(error)[:500]}")
            if len(messages) >= 20:
                return messages
    return messages


class PipelineService:
    """Own pipeline artifacts and prevent stages from being skipped."""

    def __init__(self, work_root: Path) -> None:
        self.work_root = work_root.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._background_jobs: dict[tuple[str, int], threading.Thread] = {}
        self.stores = StoreRegistry(self.work_root / ".ozon-stores.json")
        self.ai_provider = AiProviderRegistry(self.work_root / ".ai-provider.json")
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        """Turn orphaned RUNNING states into explicit failures after a restart."""

        for directory in self.work_root.iterdir():
            state_path = directory / "pipeline.json"
            if not directory.is_dir() or not state_path.exists():
                continue
            try:
                state = _read_object(state_path)
            except PipelineError:
                continue
            changed = False
            for stage in (state.get("stages") or {}).values():
                if isinstance(stage, dict) and stage.get("status") == "RUNNING":
                    stage["status"] = "FAIL"
                    stage["checked_at"] = _now()
                    stage["failures"] = ["服务器重启中断了后台任务，请重新运行该阶段"]
                    changed = True
            if changed:
                state["updated_at"] = _now()
                _atomic_json(state_path, state)

    def ai_provider_status(self) -> dict[str, Any]:
        return self.ai_provider.public_status()

    def configure_ai_provider(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.ai_provider.save(value)
        except AiProviderConfigurationError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def delete_ai_provider(self) -> None:
        self.ai_provider.delete()

    def list_stores(self) -> list[dict[str, Any]]:
        return self.stores.list_public()

    def authorize_store(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.stores.authorize(value)
        except StoreAuthorizationError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def verify_store(self, store_id: str) -> dict[str, Any]:
        try:
            return self.stores.verify(store_id)
        except StoreAuthorizationError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def delete_store(self, store_id: str) -> None:
        try:
            self.stores.delete(store_id)
        except StoreAuthorizationError as exc:
            raise PipelineError(str(exc), status=404) from exc

    def _selected_store(
        self, pipeline_id: str, requested_store_id: Any = None
    ) -> dict[str, Any]:
        selected = str(requested_store_id or "").strip()
        state_path = self._state_path(pipeline_id)
        if not selected and state_path.exists():
            selected = str(_read_object(state_path).get("store_id") or "").strip()
        public_stores = self.stores.list_public()
        if not selected and len(public_stores) == 1:
            selected = str(public_stores[0]["store_id"])
        if selected:
            try:
                return self.stores.credentials(selected)
            except StoreAuthorizationError as exc:
                raise PipelineError(str(exc), status=409) from exc
        if os.environ.get("OZON_CLIENT_ID") and os.environ.get("OZON_API_KEY"):
            currency = str(os.environ.get("OZON_CURRENCY") or "").strip().upper()
            if currency not in {"RUB", "CNY"}:
                raise PipelineError("OZON_CURRENCY 仅支持 RUB 或 CNY", status=409)
            return {
                "store_id": "ENV_DEFAULT",
                "name": "环境变量店铺",
                "client_id": os.environ["OZON_CLIENT_ID"],
                "api_key": os.environ["OZON_API_KEY"],
                "currency_code": currency,
                "credential_source": "environment",
            }
        raise PipelineError("请先在“店铺授权”中添加并验证 OZON 店铺", status=409)

    def _assign_store(self, pipeline_id: str, store_id: str) -> None:
        path = self._state_path(pipeline_id)
        state = _read_object(path)
        state["store_id"] = store_id
        state["updated_at"] = _now()
        _atomic_json(path, state)

    def directory(self, pipeline_id: Any) -> Path:
        return self.work_root / _safe_pipeline_id(pipeline_id)

    def path(self, pipeline_id: Any, stage: int) -> Path:
        if stage not in STAGE_FILES:
            raise PipelineError("无效阶段")
        return self.directory(pipeline_id) / STAGE_FILES[stage]

    def _state_path(self, pipeline_id: Any) -> Path:
        return self.directory(pipeline_id) / "pipeline.json"

    def _image_selection_path(self, pipeline_id: Any) -> Path:
        return self.directory(pipeline_id) / IMAGE_SELECTION_FILE

    def _selected_image_items(
        self, pipeline_id: str, source: Mapping[str, Any]
    ) -> list[Mapping[str, Any]]:
        items = _image_items(source)
        path = self._image_selection_path(pipeline_id)
        if not path.exists():
            return [{**item, "action": "translate"} for item in items]
        selection = _read_object(path)
        image_plan = selection.get("image_plan")
        if isinstance(image_plan, list):
            actions = {
                str(entry.get("url")): str(entry.get("action"))
                for entry in image_plan
                if isinstance(entry, Mapping)
            }
            return [
                {**item, "action": actions.get(str(item.get("url")), "exclude")}
                for item in items
                if actions.get(str(item.get("url")), "exclude") != "exclude"
            ]
        selected_urls = selection.get("selected_urls")
        if not isinstance(selected_urls, list):
            raise PipelineError(f"{IMAGE_SELECTION_FILE} 的 selected_urls 必须是数组")
        selected = {str(url) for url in selected_urls}
        return [
            {**item, "action": "translate"}
            for item in items
            if str(item.get("url")) in selected
        ]

    def list_images(self, pipeline_id: Any) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        raw = self.read_artifact(pipeline_id, 1)
        items = _image_items(raw)
        selected_items = self._selected_image_items(pipeline_id, raw)
        actions = {
            str(item.get("url")): str(item.get("action")) for item in selected_items
        }
        images = [
            {
                "url": str(item.get("url")),
                "source_role": item.get("source_role"),
                "sku_id": item.get("sku_id"),
                "width": item.get("width"),
                "height": item.get("height"),
                "action": actions.get(str(item.get("url")), "exclude"),
                "selected": actions.get(str(item.get("url")), "exclude")
                != "exclude",
            }
            for item in items
        ]
        ai_calls = sum(
            8 if item["action"] == "reference_8" else 1
            for item in images
            if item["action"] in {"reference_8", "sku_main"}
        )
        translation_calls = sum(
            1 for item in images if item["action"] == "translate"
        )
        return {
            "pipeline_id": pipeline_id,
            "images": images,
            "total_count": len(images),
            "selected_count": sum(1 for item in images if item["selected"]),
            "estimated_ai_calls": ai_calls,
            "estimated_translation_calls": translation_calls,
        }

    def save_image_selection(
        self, pipeline_id: Any, image_plan: Any
    ) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        if not isinstance(image_plan, list) or any(
            not isinstance(entry, Mapping) for entry in image_plan
        ):
            raise PipelineError("image_plan 必须是逐图处理方案数组")
        with self._lock:
            running = self._background_jobs.get((pipeline_id, 3))
            if running is not None and running.is_alive():
                raise PipelineError("阶段 3 正在处理图片，完成后才能修改清单", status=409)
            raw = self.read_artifact(pipeline_id, 1)
            items = _image_items(raw)
            by_url = {str(item.get("url")): item for item in items}
            requested: dict[str, str] = {}
            for entry in image_plan:
                url = str(entry.get("url") or "").strip()
                action = str(entry.get("action") or "exclude").strip()
                if url in requested:
                    raise PipelineError("同一图片不能配置多次")
                requested[url] = action
            if any(url not in by_url for url in requested):
                raise PipelineError("图片清单包含不属于当前商品的 URL")
            if any(action not in IMAGE_ACTIONS for action in requested.values()):
                raise PipelineError("图片处理方式无效")
            if sum(1 for action in requested.values() if action == "reference_8") > 1:
                raise PipelineError("只能选择一张图片作为共用八图参考")
            if any(
                action == "sku_main"
                and by_url[url].get("source_role") != "sku_image"
                for url, action in requested.items()
            ):
                raise PipelineError("只有 SKU 图片可以选择生成 SKU 专属主图")
            active_actions = {
                action for action in requested.values() if action != "exclude"
            }
            if not active_actions:
                raise PipelineError("至少需要选择一张图片进行处理")
            if not active_actions.intersection({"reference_8", "sku_main"}):
                raise PipelineError("至少选择一张八图参考或 SKU 主图，才能生成商品图库")
            ordered_plan = [
                {
                    "url": str(item.get("url")),
                    "action": requested.get(str(item.get("url")), "exclude"),
                    "source_role": item.get("source_role"),
                    "sku_id": item.get("sku_id"),
                }
                for item in items
            ]
            _atomic_json(
                self._image_selection_path(pipeline_id),
                {
                    "schema_version": "1.0",
                    "source_offer_id": pipeline_id,
                    "image_plan": ordered_plan,
                    "selected_urls": [
                        item["url"]
                        for item in ordered_plan
                        if item["action"] != "exclude"
                    ],
                    "updated_at": _now(),
                },
            )
            (self.directory(pipeline_id) / "03_media_progress.json").unlink(
                missing_ok=True
            )
            state_path = self._state_path(pipeline_id)
            state = _read_object(state_path)
            for stage_number in range(3, 6):
                stage = (state.get("stages") or {}).get(str(stage_number))
                if isinstance(stage, dict):
                    stage["status"] = "STALE"
                    stage["stale_after_image_selection"] = True
                    stage["checked_at"] = _now()
            state["updated_at"] = _now()
            _atomic_json(state_path, state)
        return self.list_images(pipeline_id)

    def _record(self, pipeline_id: str, stage: int, passed: bool, failures: list[str]) -> None:
        path = self._state_path(pipeline_id)
        state = _read_object(path) if path.exists() else {
            "pipeline_id": pipeline_id,
            "created_at": _now(),
            "stages": {},
        }
        state["updated_at"] = _now()
        # Preserve artifacts (especially upload logs) for audit and duplicate
        # protection, while making every downstream result unusable after a rerun.
        for downstream in range(stage + 1, 6):
            existing = state["stages"].get(str(downstream))
            if isinstance(existing, dict):
                existing["status"] = "STALE"
                existing["stale_after_stage"] = stage
                existing["checked_at"] = _now()
        state["stages"][str(stage)] = {
            "status": "PASS" if passed else "FAIL",
            "file": STAGE_FILES[stage],
            "checked_at": _now(),
            "failures": failures,
        }
        _atomic_json(path, state)

    def _record_running(self, pipeline_id: str, stage: int) -> None:
        path = self._state_path(pipeline_id)
        state = _read_object(path)
        state["updated_at"] = _now()
        for downstream in range(stage + 1, 6):
            existing = state.get("stages", {}).get(str(downstream))
            if isinstance(existing, dict):
                existing["status"] = "STALE"
                existing["stale_after_stage"] = stage
                existing["checked_at"] = _now()
        state.setdefault("stages", {})[str(stage)] = {
            "status": "RUNNING",
            "file": STAGE_FILES[stage],
            "checked_at": _now(),
            "failures": [],
        }
        _atomic_json(path, state)

    def _require_previous(self, pipeline_id: str, stage: int) -> None:
        if stage <= 1:
            return
        state_path = self._state_path(pipeline_id)
        state = _read_object(state_path)
        previous = state.get("stages", {}).get(str(stage - 1), {})
        if previous.get("status") != "PASS" or not self.path(pipeline_id, stage - 1).exists():
            raise PipelineError(f"阶段 {stage - 1} 未通过门禁，禁止进入阶段 {stage}", status=409)

    def list_pipelines(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(self.work_root.iterdir(), reverse=True):
            if not path.is_dir() or not _PIPELINE_ID.fullmatch(path.name):
                continue
            state_path = path / "pipeline.json"
            if state_path.exists():
                try:
                    result.append(_read_object(state_path))
                except PipelineError:
                    continue
        return result

    def get_pipeline(self, pipeline_id: Any) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        path = self._state_path(pipeline_id)
        if not path.exists():
            raise PipelineError("商品工作区不存在", status=404)
        state = _read_object(path)
        state["artifacts"] = {
            str(stage): STAGE_FILES[stage]
            for stage in STAGE_FILES
            if self.path(pipeline_id, stage).exists()
        }
        return state

    def read_artifact(self, pipeline_id: Any, stage: int) -> dict[str, Any]:
        return _read_object(self.path(pipeline_id, stage))

    def run_stage_1(self, offer_id: Any) -> dict[str, Any]:
        try:
            pipeline_id = _safe_pipeline_id(extract_offer_id(offer_id))
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc
        alpha_access_key = os.environ.get("ALPHASHOP_ACCESS_KEY")
        alpha_secret_key = os.environ.get("ALPHASHOP_SECRET_KEY")
        missing = []
        if not alpha_access_key:
            missing.append("ALPHASHOP_ACCESS_KEY")
        if not alpha_secret_key:
            missing.append("ALPHASHOP_SECRET_KEY")
        if missing:
            raise PipelineError(f"缺少环境变量：{', '.join(missing)}", status=409)
        with self._lock:
            client = AlphaShopClient.from_environment()
            try:
                exchange = client.offer_detail_exchange(pipeline_id)
            except AlphaShopInsufficientPointsError as exc:
                failures = [str(exc)]
                self._record(pipeline_id, 1, False, failures)
                raise PipelineError(str(exc), status=402) from exc
            except AlphaShopHttpError as exc:
                failures = [
                    f"HTTP {exc.status_code}；响应正文前 500 字符：{exc.response_preview}"
                ]
                self._record(pipeline_id, 1, False, failures)
                raise PipelineError(failures[0], status=502) from exc
            except AlphaShopApiError as exc:
                failures = [str(exc)]
                self._record(pipeline_id, 1, False, failures)
                raise PipelineError(str(exc), status=502) from exc
            raw = normalize_alphashop_offer(exchange.payload, pipeline_id)
            failures = gate_stage_1(raw)
            self._image_selection_path(pipeline_id).unlink(missing_ok=True)
            (self.directory(pipeline_id) / "03_media_progress.json").unlink(
                missing_ok=True
            )
            _atomic_json(self.path(pipeline_id, 1), raw)
            self._record(pipeline_id, 1, not failures, failures)
            return {
                "pipeline_id": pipeline_id,
                "stage": 1,
                "status": "PASS" if not failures else "FAIL",
                "failures": failures,
                "artifact": STAGE_FILES[1],
                "alphashop_diagnostics": {
                    "jwt_payload": exchange.jwt_payload_redacted,
                    "response_preview": exchange.response_preview,
                },
            }

    def run_stage_2(
        self,
        pipeline_id: Any,
        *,
        confidence_threshold: Any = CONFIDENCE_THRESHOLD,
        store_id: Any = None,
    ) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 2)
        authorized_store = self._selected_store(pipeline_id, store_id)
        try:
            threshold = float(confidence_threshold)
        except (TypeError, ValueError) as exc:
            raise PipelineError("置信度阈值必须是数字") from exc
        if not 0 <= threshold <= 1:
            raise PipelineError("置信度阈值必须在 0–1 之间")
        with self._lock:
            raw = self.read_artifact(pipeline_id, 1)
            source_category_id = raw.get("category_id")
            try:
                category_exchange = AlphaShopClient.from_environment().category_mapping(
                    str(source_category_id or "")
                )
                preferred_pair = extract_external_category_pair(category_exchange.payload)
            except AlphaShopInsufficientPointsError as exc:
                raise PipelineError(str(exc), status=402) from exc
            except (AlphaShopApiError, ValueError) as exc:
                raise PipelineError(f"AlphaShop 类目映射失败：{exc}", status=502) from exc
            provider = LiveMetadataProvider(
                OzonDescriptionCategoryClient(
                    authorized_store["client_id"], authorized_store["api_key"]
                )
            )
            mapped = map_listing(
                _source_for_mapper(raw),
                provider,
                confidence_threshold=threshold,
                preferred_category_type=preferred_pair,
            )
            mapped["category_search_input"] = {
                "category_id": raw.get("category_id"),
                "category_path_zh": raw.get("category_path_zh"),
                "title_zh": raw.get("title_zh"),
            }
            canonical_mapping = json.dumps(
                category_exchange.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            mapped["alphashop_category_mapping_provenance"] = {
                "endpoint": "/alphashop.openclaw.offer.cate.ozon.query/1.0",
                "request": {"categoryId": str(source_category_id)},
                "response_sha256": hashlib.sha256(canonical_mapping).hexdigest(),
                "validated_pair": {
                    "ozon_category_id": preferred_pair[0],
                    "ozon_type_id": preferred_pair[1],
                },
            }
            failures = gate_stage_2(mapped)
            _atomic_json(self.path(pipeline_id, 2), mapped)
            self._record(pipeline_id, 2, not failures, failures)
            self._assign_store(pipeline_id, str(authorized_store["store_id"]))
            return {"stage": 2, "status": "PASS" if not failures else "FAIL", "failures": failures, "artifact": STAGE_FILES[2]}

    def run_stage_3(
        self,
        pipeline_id: Any,
        *,
        media_manifest: Any = None,
        media_mode: Any = "alphashop_translate",
        text_mode: Any = "built_in",
    ) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 3)
        if media_manifest is not None:
            raise PipelineError("图片必须通过已配置的 API 处理，不接受人工清单绕过")
        media_mode = str(media_mode or "").strip().lower()
        text_mode = str(text_mode or "").strip().lower()
        if media_mode not in {"alphashop_translate", "ai_generate", "image_plan"}:
            raise PipelineError("图片模式必须是逐图方案、AlphaShop 翻译或 AI 生图")
        if text_mode not in {"built_in", "ai_generate"}:
            raise PipelineError("文本模式必须是内置规则或 AI 生成")
        with self._lock:
            raw = self.read_artifact(pipeline_id, 1)
            mapped = self.read_artifact(pipeline_id, 2)
            progress_path = self.directory(pipeline_id) / "03_media_progress.json"
            try:
                if media_mode == "image_plan":
                    planned_items = self._selected_image_items(pipeline_id, raw)
                    plan_sha256 = hashlib.sha256(
                        json.dumps(
                            planned_items,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    existing_results: list[Mapping[str, Any]] = []
                    if progress_path.exists():
                        previous_progress = _read_object(progress_path)
                        if previous_progress.get("plan_sha256") == plan_sha256:
                            existing_results = [
                                row
                                for row in previous_progress.get("results") or []
                                if isinstance(row, Mapping)
                            ]

                    def save_progress(rows: Any) -> None:
                        _atomic_json(
                            progress_path,
                            {
                                "schema_version": "1.0",
                                "source_offer_id": pipeline_id,
                                "updated_at": _now(),
                                "plan_sha256": plan_sha256,
                                "results": list(rows),
                            },
                        )

                    inspection = process_image_plan(
                        planned_items,
                        self.ai_provider.client(),
                        AlphaShopClient.from_environment(),
                        raw,
                        progress=save_progress,
                        existing_results=existing_results,
                    )
                elif media_mode == "ai_generate":
                    relay = self.ai_provider.client()
                    inspection = generate_all_images(
                        self._selected_image_items(pipeline_id, raw), relay, raw
                    )
                else:
                    inspection = translate_all_images(
                        self._selected_image_items(pipeline_id, raw),
                        AlphaShopClient.from_environment(),
                    )
            except AlphaShopInsufficientPointsError as exc:
                failures = [str(exc)]
                self._record(pipeline_id, 3, False, failures)
                raise PipelineError(str(exc), status=402) from exc
            except (AiRelayError, AiProviderConfigurationError) as exc:
                progress_note = ""
                if progress_path.exists():
                    progress_value = _read_object(progress_path)
                    progress_rows = progress_value.get("results") or []
                    succeeded = sum(
                        1
                        for row in progress_rows
                        if isinstance(row, Mapping) and row.get("final_public_url")
                    )
                    progress_note = (
                        f"；本轮生图/翻译已成功 {succeeded} 张，"
                        f"已记录 {len(progress_rows)} 条进度"
                    )
                failures = [f"{exc}{progress_note}"]
                self._record(pipeline_id, 3, False, failures)
                raise PipelineError(failures[0], status=502) from exc
            generated_copy = None
            copy_fallback_reason = None
            if text_mode == "ai_generate":
                try:
                    generated_copy = self.ai_provider.client().generate_listing_copy(
                        _source_for_mapper(raw), mapped
                    )
                except (AiRelayError, AiProviderConfigurationError) as exc:
                    # Image generation may already have incurred a charge.  A text
                    # provider outage must not discard that completed work or block
                    # the deterministic, strictly validated built-in copy fallback.
                    copy_fallback_reason = str(exc)[:800]
            final = build_final_listing(
                mapped,
                _source_for_mapper(raw),
                inspection,
                generated_copy=generated_copy,
            )
            final["generation_options"] = {
                "media_mode": media_mode,
                "text_mode": text_mode,
                "effective_text_mode": (
                    "built_in_fallback" if copy_fallback_reason else text_mode
                ),
                "copy_fallback_reason": copy_fallback_reason,
                "ai_relay": self.ai_provider.public_status(),
            }
            final["localized_images"] = [
                {
                    "source_url": item.get("source_url"),
                    "final_public_url": item.get("final_public_url"),
                    "contains_chinese": item.get("contains_chinese"),
                    "translation_status": item.get("translation_status"),
                    "plan_action": item.get("plan_action"),
                    "module_number": item.get("module_number"),
                    "image_usage": item.get("image_usage"),
                    "sku_id": item.get("sku_id"),
                    "shared_across_skus": item.get("shared_across_skus"),
                    "error": item.get("error"),
                }
                for item in inspection
            ]
            failures = gate_stage_3(final)
            _atomic_json(self.path(pipeline_id, 3), final)
            self._record(pipeline_id, 3, not failures, failures)
            return {
                "stage": 3,
                "status": "PASS" if not failures else "FAIL",
                "failures": failures,
                "warnings": (
                    [f"AI 文本不可用，已自动使用内置俄语文案：{copy_fallback_reason}"]
                    if copy_fallback_reason
                    else []
                ),
                "artifact": STAGE_FILES[3],
            }

    def start_stage_3(
        self,
        pipeline_id: Any,
        *,
        media_manifest: Any = None,
        media_mode: Any = "alphashop_translate",
        text_mode: Any = "built_in",
    ) -> dict[str, Any]:
        """Start the slow image/content stage without holding an HTTP request open."""

        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 3)
        normalized_media_mode = str(media_mode or "").strip().lower()
        normalized_text_mode = str(text_mode or "").strip().lower()
        if media_manifest is not None:
            raise PipelineError("图片必须通过已配置的 API 处理，不接受人工清单绕过")
        if normalized_media_mode not in {
            "alphashop_translate",
            "ai_generate",
            "image_plan",
        }:
            raise PipelineError("图片模式必须是逐图方案、AlphaShop 翻译或 AI 生图")
        if normalized_text_mode not in {"built_in", "ai_generate"}:
            raise PipelineError("文本模式必须是内置规则或 AI 生成")
        if normalized_media_mode == "image_plan" and not self._image_selection_path(
            pipeline_id
        ).exists():
            raise PipelineError("请先在阶段 1 的“图片”中保存逐图处理方案", status=409)

        key = (pipeline_id, 3)
        with self._lock:
            existing = self._background_jobs.get(key)
            if existing is not None and existing.is_alive():
                raise PipelineError("阶段 3 正在后台执行，请等待完成", status=409)
            self._record_running(pipeline_id, 3)

            def worker() -> None:
                try:
                    self.run_stage_3(
                        pipeline_id,
                        media_manifest=None,
                        media_mode=normalized_media_mode,
                        text_mode=normalized_text_mode,
                    )
                except PipelineError as exc:
                    self._record(pipeline_id, 3, False, [str(exc)[:500]])
                except Exception as exc:  # keep unexpected worker failures auditable
                    self._record(
                        pipeline_id,
                        3,
                        False,
                        [f"阶段 3 后台执行失败：{str(exc)[:400]}"],
                    )
                finally:
                    with self._lock:
                        self._background_jobs.pop(key, None)

            thread = threading.Thread(
                target=worker,
                name=f"ozon-stage-3-{pipeline_id}",
                daemon=True,
            )
            self._background_jobs[key] = thread
            thread.start()
        return {
            "stage": 3,
            "status": "RUNNING",
            "failures": [],
            "artifact": STAGE_FILES[3],
        }

    def run_stage_4(self, pipeline_id: Any, *, store_config: Any) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 4)
        if not isinstance(store_config, Mapping):
            raise PipelineError("店铺配置必须是 JSON 对象")
        authorized_store = self._selected_store(pipeline_id, store_config.get("store_id"))
        ozon_currency = str(authorized_store["currency_code"]).strip().upper()
        with self._lock:
            raw = self.read_artifact(pipeline_id, 1)
            final = self.read_artifact(pipeline_id, 3)
            final, override_provenance = self._apply_required_attribute_overrides(
                final,
                store_config.get("attribute_overrides") or {},
                authorized_store,
            )
            effective_store_config = {
                **dict(store_config),
                "store_id": authorized_store["store_id"],
                "credential_prefix": authorized_store["store_id"],
                "currency_code": ozon_currency,
            }
            stores = load_store_configs({"stores": [effective_store_config]})
            assembly = assemble_import_payload(final, _source_for_mapper(raw), stores[0])
            failures = list(assembly.validation_errors) + gate_stage_4(assembly.payload)
            if any(
                item.get("currency_code") != ozon_currency
                for item in assembly.payload.get("items") or []
                if isinstance(item, Mapping)
            ):
                failures.append("payload currency_code 与 OZON_CURRENCY 不一致")
            _atomic_json(self.path(pipeline_id, 4), assembly.payload)
            _atomic_json(self.directory(pipeline_id) / "04_context.json", {
                "store_config": effective_store_config,
                "credential_source": "environment"
                if authorized_store.get("credential_source") == "environment"
                else "registry",
                "offer_ids": assembly.offer_ids,
                "required_attribute_ids": assembly.required_attribute_ids,
                "dimension_source": assembly.dimension_source,
                "warnings": assembly.warnings,
                "validation_errors": assembly.validation_errors,
                "attribute_override_provenance": override_provenance,
            })
            self._record(pipeline_id, 4, not failures, failures)
            self._assign_store(pipeline_id, str(authorized_store["store_id"]))
            return {"stage": 4, "status": "PASS" if not failures else "FAIL", "failures": failures, "warnings": assembly.warnings, "artifact": STAGE_FILES[4]}

    def required_attribute_options(
        self, pipeline_id: Any, *, store_id: Any = None
    ) -> dict[str, Any]:
        """Return every category attribute without eagerly downloading dictionaries."""

        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 4)
        store = self._selected_store(pipeline_id, store_id)
        final = self.read_artifact(pipeline_id, 3)
        category_id = final.get("ozon_category_id")
        type_id = final.get("ozon_type_id")
        if not isinstance(category_id, int) or not isinstance(type_id, int):
            raise PipelineError("阶段 3 缺少 Ozon category/type", status=409)
        client = OzonDescriptionCategoryClient(store["client_id"], store["api_key"])
        try:
            chinese_attribute_names = _ozon_attribute_names(
                client.category_attributes(category_id, type_id, language="ZH_HANS")
            )
            metadata_language = "ZH_HANS"
        except OzonApiError:
            # Editing must remain available during a temporary metadata outage.
            # Known labels still receive deterministic Chinese fallbacks.
            chinese_attribute_names = {}
            metadata_language = "ZH_FALLBACK"
        previous: dict[int, Mapping[str, Any]] = {}
        context_path = self.directory(pipeline_id) / "04_context.json"
        if context_path.exists():
            context = _read_object(context_path)
            previous = {
                item["attribute_id"]: item
                for item in context.get("attribute_override_provenance") or []
                if isinstance(item, Mapping)
                and isinstance(item.get("attribute_id"), int)
            }
        attributes: list[dict[str, Any]] = []
        unresolved_ids = {
            item.get("attribute_id") for item in _missing_required_attributes(final)
        }
        for item in final.get("ozon_attributes") or []:
            if not isinstance(item, Mapping):
                continue
            attribute_id = item.get("attribute_id")
            if not isinstance(attribute_id, int):
                continue
            matches = [
                match
                for match in item.get("dictionary_matches") or []
                if isinstance(match, Mapping)
                and isinstance(match.get("dictionary_value_id"), int)
            ]
            prior = previous.get(attribute_id, {})
            selected_id = prior.get("dictionary_value_id")
            selected_value = prior.get("value")
            if selected_id is None and matches:
                selected_id = matches[0].get("dictionary_value_id")
                selected_value = matches[0].get("dictionary_value")
            if selected_value is None:
                selected_value = item.get("value")
            attributes.append(
                {
                    "attribute_id": attribute_id,
                    "attribute_name_ru": item.get("attribute_name_ru"),
                    "attribute_name_zh": chinese_attribute_names.get(attribute_id)
                    or _attribute_name_zh_fallback(
                        item.get("attribute_name_ru"), attribute_id
                    ),
                    "attribute_type": item.get("attribute_type"),
                    "is_required": bool(item.get("is_required")),
                    "is_dictionary": bool(item.get("is_dictionary")),
                    "mapping_status": item.get("mapping_status"),
                    "selected_dictionary_value_id": selected_id,
                    "selected_value": selected_value,
                    "override_required": (
                        attribute_id in previous or attribute_id in unresolved_ids
                    ),
                }
            )
        return {
            "pipeline_id": pipeline_id,
            "ozon_category_id": category_id,
            "ozon_type_id": type_id,
            "metadata_language": metadata_language,
            "attributes": attributes,
            "blocking_confirmation_count": len(_blocking_confirmations(final)),
        }

    def attribute_dictionary_options(
        self, pipeline_id: Any, attribute_id: Any, *, store_id: Any = None
    ) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 4)
        try:
            requested_id = int(attribute_id)
        except (TypeError, ValueError) as exc:
            raise PipelineError("属性 ID 无效") from exc
        store = self._selected_store(pipeline_id, store_id)
        final = self.read_artifact(pipeline_id, 3)
        attribute = next(
            (
                item
                for item in final.get("ozon_attributes") or []
                if isinstance(item, Mapping)
                and item.get("attribute_id") == requested_id
            ),
            None,
        )
        if attribute is None or not attribute.get("is_dictionary"):
            raise PipelineError("该属性不是当前类目的字典特征", status=404)
        category_id = final.get("ozon_category_id")
        type_id = final.get("ozon_type_id")
        if not isinstance(category_id, int) or not isinstance(type_id, int):
            raise PipelineError("阶段 3 缺少 Ozon category/type", status=409)
        try:
            response = OzonDescriptionCategoryClient(
                store["client_id"], store["api_key"]
            ).attribute_values(
                category_id,
                type_id,
                requested_id,
                language="ZH_HANS",
            )
        except OzonApiError as exc:
            raise PipelineError(f"Ozon 字典值查询失败：{exc}", status=502) from exc
        return {
            "attribute_id": requested_id,
            "attribute_name_ru": attribute.get("attribute_name_ru"),
            "attribute_name_zh": _attribute_name_zh_fallback(
                attribute.get("attribute_name_ru"), requested_id
            ),
            "language": "ZH_HANS",
            "options": _ozon_dictionary_rows(response),
        }

    def _apply_required_attribute_overrides(
        self,
        final: Mapping[str, Any],
        overrides: Any,
        store: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(overrides, Mapping):
            raise PipelineError("attribute_overrides 必须是对象")
        resolved = deepcopy(dict(final))
        rich_value = resolved.get("rich_content_json")
        if rich_value is not None:
            description = str(resolved.get("description_ru") or "Описание товара")
            try:
                upgraded_rich = upgrade_legacy_rich_content(
                    rich_value,
                    fallback_title="Подробности товара",
                    fallback_text=description.split("\n\n", 1)[0][:500],
                )
            except ValueError:
                # The stage-3 gate and assembler report the precise structural
                # problem; do not hide an unrelated malformed payload here.
                pass
            else:
                resolved["rich_content_json"] = upgraded_rich
                for attribute in resolved.get("ozon_attributes") or []:
                    if not isinstance(attribute, dict):
                        continue
                    name = str(attribute.get("attribute_name_ru") or "").lower()
                    if ("rich" in name or "рич" in name) and "контент" in name:
                        serialized = json.dumps(
                            upgraded_rich,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        attribute["value"] = serialized
                        attribute["source_refs"] = [
                            {
                                "location": "stage_4.legacy_rich_content_upgrade",
                                "value": serialized,
                            }
                        ]
        known = {
            item.get("attribute_id"): item
            for item in resolved.get("ozon_attributes") or []
            if isinstance(item, Mapping)
            if isinstance(item.get("attribute_id"), int)
        }
        unknown = {
            str(key)
            for key in overrides
            if not str(key).isdigit() or int(str(key)) not in known
        }
        if unknown:
            raise PipelineError(f"包含非当前类目的属性：{', '.join(sorted(unknown))}")
        chinese_fields: list[dict[str, Any]] = []
        for raw_attribute_id, raw_override in overrides.items():
            if not isinstance(raw_override, Mapping):
                continue
            attribute_id = int(str(raw_attribute_id))
            attribute = known[attribute_id]
            if attribute.get("is_dictionary"):
                continue
            raw_value = str(raw_override.get("value") or "").strip()
            if not re.search(r"[\u3400-\u9fff]", raw_value):
                continue
            attribute_type = str(attribute.get("attribute_type") or "").lower()
            if any(
                token in attribute_type
                for token in ("integer", "decimal", "float", "double", "number")
            ):
                raise PipelineError(
                    f"数值特征 {attribute_id} 请只填写数字，不要填写中文单位"
                )
            name = str(attribute.get("attribute_name_ru") or "").lower()
            if ("rich" in name or "рич" in name) and "контент" in name:
                raise PipelineError("Rich Content JSON 不能作为普通中文文本翻译")
            chinese_fields.append(
                {
                    "attribute_id": attribute_id,
                    "attribute_name_ru": attribute.get("attribute_name_ru"),
                    "value_zh": raw_value,
                }
            )
        translated_values: dict[str, str] = {}
        if chinese_fields:
            try:
                translated_values = self.ai_provider.client().translate_attribute_values(
                    chinese_fields
                )
            except (AiRelayError, AiProviderConfigurationError) as exc:
                raise PipelineError(
                    f"中文特征转俄语失败，未组装上架载荷：{exc}",
                    status=502,
                ) from exc
        category_id = resolved.get("ozon_category_id")
        type_id = resolved.get("ozon_type_id")
        client = OzonDescriptionCategoryClient(store["client_id"], store["api_key"])
        provenance: list[dict[str, Any]] = []
        resolved_ids: set[int] = set()
        by_id = {
            item.get("attribute_id"): item
            for item in resolved.get("ozon_attributes") or []
            if isinstance(item, dict) and isinstance(item.get("attribute_id"), int)
        }
        for raw_attribute_id, raw_override in overrides.items():
            attribute_id = int(str(raw_attribute_id))
            if not isinstance(raw_override, Mapping):
                raise PipelineError(f"属性 {attribute_id} 的补充值格式错误")
            attribute = by_id[attribute_id]
            if attribute.get("is_dictionary"):
                raw_value_id = raw_override.get("dictionary_value_id")
                try:
                    value_id = int(raw_value_id)
                except (TypeError, ValueError) as exc:
                    raise PipelineError(f"字典属性 {attribute_id} 必须选择合法值") from exc
                if not isinstance(category_id, int) or not isinstance(type_id, int):
                    raise PipelineError("无法校验字典值：category/type 缺失")
                try:
                    rows = _ozon_dictionary_rows(
                        client.attribute_values(category_id, type_id, attribute_id)
                    )
                except OzonApiError as exc:
                    raise PipelineError(
                        f"Ozon 字典属性 {attribute_id} 校验失败：{exc}", status=502
                    ) from exc
                legal = next(
                    (row for row in rows if row["dictionary_value_id"] == value_id),
                    None,
                )
                if legal is None:
                    raise PipelineError(
                        f"字典属性 {attribute_id} 的值 ID 不在本次 Ozon API 返回范围内"
                    )
                attribute["value"] = legal["value"]
                attribute["dictionary_value_id"] = value_id
                attribute["dictionary_matches"] = [
                    {
                        "source_value": "ERP_MANUAL_SELECTION",
                        "dictionary_value": legal["value"],
                        "dictionary_value_id": value_id,
                        "confidence": 1.0,
                        "reason": "用户从本次 Ozon API 返回的合法字典值中选择",
                    }
                ]
                provenance.append(
                    {
                        "attribute_id": attribute_id,
                        "dictionary_value_id": value_id,
                        "value": legal["value"],
                        "endpoint": OzonDescriptionCategoryClient.ATTRIBUTE_VALUES_PATH,
                        "validated_live": True,
                    }
                )
            else:
                value_zh = str(raw_override.get("value") or "").strip()
                value = translated_values.get(str(attribute_id), value_zh)
                if not value:
                    raise PipelineError(f"必填属性 {attribute_id} 不能为空")
                attribute["value"] = value
                provenance.append(
                    {
                        "attribute_id": attribute_id,
                        "value": value,
                        "value_zh": value_zh if value != value_zh else None,
                        "source": (
                            "ERP_MANUAL_INPUT_ZH_TRANSLATED"
                            if value != value_zh
                            else "ERP_MANUAL_INPUT"
                        ),
                    }
                )
            attribute["mapping_status"] = "MAPPED_MANUAL_VALIDATED"
            attribute["mapping_confidence"] = 1.0
            attribute["source_refs"] = [
                {"location": "stage_4.attribute_overrides", "value": attribute["value"]}
            ]
            resolved_ids.add(attribute_id)
        resolved["needs_confirmation"] = [
            item
            for item in resolved.get("needs_confirmation") or []
            if not (
                isinstance(item, Mapping)
                and item.get("attribute_id") in resolved_ids
            )
        ]
        if not _blocking_confirmations(resolved) and not gate_stage_3(resolved):
            resolved["listing_readiness"] = "READY_FOR_PAYLOAD_ASSEMBLY"
        return resolved, provenance

    def _assembly_from_files(self, pipeline_id: str) -> tuple[AssemblyResult, Any]:
        payload = self.read_artifact(pipeline_id, 4)
        context = _read_object(self.directory(pipeline_id) / "04_context.json")
        store = load_store_configs({"stores": [context["store_config"]]})[0]
        assembly = AssemblyResult(
            payload=payload,
            offer_ids=list(context.get("offer_ids") or []),
            validation_errors=list(context.get("validation_errors") or []),
            warnings=list(context.get("warnings") or []),
            dimension_source=context.get("dimension_source"),
            required_attribute_ids=list(context.get("required_attribute_ids") or []),
        )
        return assembly, store

    def run_stage_5(self, pipeline_id: Any, *, request_execute: bool = False) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 5)
        environment_execute = os.environ.get("DRY_RUN", "true").strip().lower() == "false"
        execute = bool(request_execute and environment_execute)
        with self._lock:
            assembly, store = self._assembly_from_files(pipeline_id)
            client = None
            if execute:
                authorized_store = self._selected_store(pipeline_id, store.store_id)
                client = OzonUploadClient(
                    authorized_store["client_id"], authorized_store["api_key"]
                )
            upload_log = UploadLog(self.path(pipeline_id, 5))
            result = run_upload_workflow(
                store=store,
                assembly=assembly,
                upload_log=upload_log,
                execute=execute,
                client=client,
            )
            if request_execute and not environment_execute:
                result["execution_guard"] = "请求了执行，但环境仍为 DRY_RUN=true；未发送请求"
            failures = []
            if not execute:
                if request_execute:
                    failures.append(
                        "服务器仍为 DRY_RUN=true，未向 Ozon 发送上架请求；"
                        "请先启用 LIVE MODE 并重启服务"
                    )
                else:
                    failures.append(
                        "未勾选“我明确要求执行真实提交”，未向 Ozon 发送上架请求"
                    )
                if result.get("request_sent") or result.get("task_id") is not None:
                    failures.append("dry-run 日志异常出现请求或 task_id")
            if execute:
                if result.get("request_sent") is not True:
                    failures.append("执行模式没有真实发送 Ozon 请求")
                if result.get("task_id") is None:
                    failures.append("执行模式未取得 task_id")
                if result.get("status") != "VERIFIED":
                    details = _upload_error_messages(result)
                    failures.extend(
                        details
                        or [f"Ozon 执行未完成：{result.get('status')}"]
                    )
                if result.get("status") in {"VERIFIED", "VERIFICATION_FAILED"} and not isinstance(
                    result.get("attribute_verification"), Mapping
                ):
                    failures.append("执行模式缺少 attribute_verification 回查结果")
            passed = not failures
            self._record(pipeline_id, 5, passed, failures)
            return {"stage": 5, "status": "PASS" if passed else "FAIL", "failures": failures, "executed": execute, "result": result, "artifact": STAGE_FILES[5]}

    def audit(self, pipeline_id: Any) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        results: list[dict[str, Any]] = []

        def check(number: int, label: str, fn: Callable[[], tuple[bool, str]]) -> None:
            try:
                passed, detail = fn()
            except (PipelineError, OSError, ValueError, KeyError, TypeError) as exc:
                passed, detail = False, str(exc)
            results.append({"number": number, "label": label, "status": "PASS" if passed else "FAIL", "detail": detail})

        def category_trace() -> tuple[bool, str]:
            raw, mapped = self.read_artifact(pipeline_id, 1), self.read_artifact(pipeline_id, 2)
            actual = mapped.get("category_search_input", {}).get("category_id")
            alpha_request = mapped.get(
                "alphashop_category_mapping_provenance", {}
            ).get("request", {})
            tree_called = any(
                call.get("endpoint") == "/v1/description-category/tree"
                for call in mapped.get("metadata_provenance", {}).get("calls", [])
                if isinstance(call, Mapping)
            )
            ok = (
                actual == raw.get("category_id")
                and alpha_request.get("categoryId") == str(raw.get("category_id"))
                and mapped.get("alphashop_category_candidate_validated") is True
                and tree_called
            )
            return ok, "category_id 驱动 AlphaShop 映射，候选已由 OZON 类目树验证" if ok else "category_id → AlphaShop → OZON 类目树链路不完整"

        def dictionary_trace() -> tuple[bool, str]:
            mapped = self.read_artifact(pipeline_id, 2)
            failures = gate_stage_2(mapped)
            trace_failures = [item for item in failures if "追溯" in item]
            return not trace_failures, "所有字典值 ID 均有 attribute/values 调用记录" if not trace_failures else "; ".join(trace_failures)

        def rich_shape() -> tuple[bool, str]:
            validate_rich_content(self.read_artifact(pipeline_id, 3).get("rich_content_json"))
            return True, "最外层为 content + version 0.3，内部为 raShowcase"

        def image_hosts() -> tuple[bool, str]:
            final = self.read_artifact(pipeline_id, 3)
            bad: list[str] = []
            for item in final.get("localized_images") or []:
                url = item.get("final_public_url") if isinstance(item, Mapping) else ""
                host = (urlparse(str(url)).hostname or "").lower()
                if host in BANNED_IMAGE_HOSTS or any(host.endswith(f".{value}") for value in BANNED_IMAGE_HOSTS):
                    bad.append(str(url))
            return not bad, "未命中禁用图床" if not bad else f"禁用 URL：{bad}"

        def attribute_completeness() -> tuple[bool, str]:
            final, payload = self.read_artifact(pipeline_id, 3), self.read_artifact(pipeline_id, 4)
            expected = {
                item.get("attribute_id") for item in final.get("ozon_attributes") or []
                if isinstance(item, Mapping)
                and (
                    item.get("dictionary_value_id") is not None
                    or item.get("value") is not None
                )
                and item.get("mapping_status") != "NEEDS_CONFIRMATION"
            }
            missing_by_item: dict[str, list[int]] = {}
            for item in payload.get("items") or []:
                actual = {attr.get("id") for attr in item.get("attributes") or [] if isinstance(attr, Mapping)}
                missing = sorted(value for value in expected - actual if isinstance(value, int))
                if missing:
                    missing_by_item[str(item.get("offer_id"))] = missing
            return not missing_by_item, "所有已确认属性均进入 payload" if not missing_by_item else f"遗漏：{missing_by_item}"

        def dry_run_log() -> tuple[bool, str]:
            log = self.read_artifact(pipeline_id, 5)
            if os.environ.get("DRY_RUN", "true").strip().lower() == "false":
                return True, "当前环境为执行模式，此项不适用"
            bad = [entry for entry in log.get("entries") or [] if entry.get("task_id") is not None or entry.get("request_sent")]
            return not bad, "DRY_RUN=true，日志无 task_id 且无请求" if not bad else "dry-run 日志出现真实任务信息"

        def hardcoded_ids() -> tuple[bool, str]:
            mapped = self.read_artifact(pipeline_id, 2)
            calls = mapped.get("metadata_provenance", {}).get("calls")
            live = mapped.get("metadata_mode") == "live" and isinstance(calls, list) and bool(calls)
            return live, "本次运行的 OZON ID 均来自实时元数据调用记录" if live else "本次运行缺少实时元数据来源证明"

        check(1, "category_id 驱动类目检索", category_trace)
        check(2, "dictionary_value_id 调用追溯", dictionary_trace)
        check(3, "Rich Content 结构", rich_shape)
        check(4, "图片图床域名", image_hosts)
        check(5, "属性全量进入 payload", attribute_completeness)
        check(6, "dry-run 无真实 task_id", dry_run_log)
        check(7, "无硬编码 OZON ID", hardcoded_ids)
        return {"pipeline_id": pipeline_id, "audited_at": _now(), "overall": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL", "checks": results}

    def config_status(self) -> dict[str, Any]:
        variables = {
            "ALPHASHOP_ACCESS_KEY": bool(os.environ.get("ALPHASHOP_ACCESS_KEY")),
            "ALPHASHOP_SECRET_KEY": bool(os.environ.get("ALPHASHOP_SECRET_KEY")),
            "DRY_RUN": bool(os.environ.get("DRY_RUN")),
            "AI_RELAY_TEXT_API_KEY": bool(os.environ.get("AI_RELAY_TEXT_API_KEY")),
            "AI_RELAY_IMAGE_API_KEY": bool(os.environ.get("AI_RELAY_IMAGE_API_KEY")),
        }
        stores = self.stores.list_public()
        return {
            "variables": variables,
            "dry_run": os.environ.get("DRY_RUN", "true").strip().lower() != "false",
            "ozon_stores": len(stores),
            "ozon_authorized": bool(stores),
            "ai_relay": self.ai_provider.public_status(),
        }
