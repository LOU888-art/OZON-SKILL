"""Five-stage pipeline orchestration and auditable gates for the web ERP."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from uuid import uuid4
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
from ozon_skill.category_mapping.mapper import (
    CONFIDENCE_THRESHOLD,
    flatten_category_types,
    map_listing,
)
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
from .activity import ActivityRegistry
from .alert_channels import AlertChannelError, AlertChannelRegistry
from .assets import AssetRegistry, AssetRegistryError, asset_id, managed_asset_id
from .finance import FinanceSettingsError, FinanceSettingsRegistry
from .notifications import NotificationRegistry
from .object_storage import ObjectStorageError, ObjectStorageRegistry
from .operations import OzonOperationsClient, OzonOperationsError
from .pricing import PricingError, calculate_pricing, calculate_target_pricing
from .stores import StoreAuthorizationError, StoreRegistry
from .team import TeamError, TeamRegistry


STAGE_FILES = {
    1: "01_raw.json",
    2: "02_mapped.json",
    3: "03_final.json",
    4: "04_payload.json",
    5: "05_upload_log.json",
}
IMAGE_SELECTION_FILE = "image-selection.json"
PRODUCT_SETTINGS_FILE = "product-settings.json"
PRODUCT_VERSIONS_FILE = "product-versions.json"
IMAGE_ACTIONS = {"exclude", "translate", "reference_8", "sku_main"}
_PIPELINE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_BATCH_JOB_ID = re.compile(r"^[a-f0-9]{32}$")
_MAX_BATCH_IMPORT_ITEMS = 100
_MAX_BATCH_PROCESS_ITEMS = 50
PLAN_CATALOG = {
    "starter": {
        "name": "启航版",
        "monthly_price_cny": 299,
        "limits": {"products": 50, "stores": 1, "ai_images": 100, "members": 1},
    },
    "growth": {
        "name": "增长版",
        "monthly_price_cny": 899,
        "limits": {"products": 500, "stores": 3, "ai_images": 1000, "members": 5},
    },
    "business": {
        "name": "企业版",
        "monthly_price_cny": 1999,
        "limits": {"products": 2000, "stores": 10, "ai_images": 5000, "members": 20},
    },
}
_CATEGORY_SEARCH_ALIASES = {
    "香薰石牌": (
        "香薰石",
        "香氛石",
        "扩香石",
        "香薰挂件",
        "香薰挂片",
        "香牌",
        "固体香薰",
        "аромакамень",
        "ароматический камень",
        "ароматическое саше",
        "ароматизатор для дома",
        "интерьерный ароматизатор",
    ),
    "香薰石": (
        "香氛石",
        "扩香石",
        "香薰挂件",
        "香薰挂片",
        "香牌",
        "固体香薰",
        "аромакамень",
        "ароматический камень",
        "ароматическое саше",
        "ароматизатор для дома",
        "интерьерный ароматизатор",
    ),
}


class PipelineError(RuntimeError):
    """Expected, user-facing pipeline failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _category_search_text(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(value or "").lower()).strip()


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
        self._batch_import_threads: dict[str, threading.Thread] = {}
        self._batch_import_root = self.work_root / ".batch-imports"
        self._batch_process_threads: dict[str, threading.Thread] = {}
        self._batch_process_root = self.work_root / ".batch-processes"
        self.stores = StoreRegistry(self.work_root / ".ozon-stores.json")
        self.ai_provider = AiProviderRegistry(self.work_root / ".ai-provider.json")
        self.team = TeamRegistry(self.work_root / ".team.json")
        self.finance = FinanceSettingsRegistry(self.work_root / ".finance-settings.json")
        self.activity = ActivityRegistry(self.work_root / ".activity.jsonl")
        self.notifications = NotificationRegistry(self.work_root / ".notifications.json")
        self.alert_channels = AlertChannelRegistry(
            self.work_root / ".alert-channels.json"
        )
        self.object_storage = ObjectStorageRegistry(self.work_root / ".object-storage.json")
        self.assets = AssetRegistry(self.work_root / ".image-assets.json")
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
        if self._batch_import_root.exists():
            for job_path in self._batch_import_root.glob("*.json"):
                try:
                    job = _read_object(job_path)
                except PipelineError:
                    continue
                if job.get("status") not in {"QUEUED", "RUNNING"}:
                    continue
                job["status"] = "INTERRUPTED"
                job["finished_at"] = _now()
                job["updated_at"] = _now()
                job["message"] = "服务器重启中断了批量采集；为避免重复扣费，系统未自动续跑"
                for item in job.get("items") or []:
                    if isinstance(item, dict) and item.get("status") in {"QUEUED", "RUNNING"}:
                        item["status"] = "INTERRUPTED"
                        item["error"] = "服务器重启中断，未自动重试"
                _atomic_json(job_path, job)
        if self._batch_process_root.exists():
            for job_path in self._batch_process_root.glob("*.json"):
                try:
                    job = _read_object(job_path)
                except PipelineError:
                    continue
                if job.get("status") not in {"QUEUED", "RUNNING"}:
                    continue
                job["status"] = "INTERRUPTED"
                job["finished_at"] = _now()
                job["updated_at"] = _now()
                job["message"] = "服务器重启中断了批量处理；为避免重复调用付费接口，系统未自动续跑"
                for item in job.get("items") or []:
                    if isinstance(item, dict) and item.get("status") in {"QUEUED", "RUNNING"}:
                        item["status"] = "INTERRUPTED"
                        item["error"] = "服务器重启中断，未自动重试"
                _atomic_json(job_path, job)

    def ai_provider_status(self) -> dict[str, Any]:
        return self.ai_provider.public_status()

    def alert_channels_status(self) -> dict[str, Any]:
        try:
            return self.alert_channels.public_status()
        except AlertChannelError as exc:
            raise PipelineError(str(exc), status=500) from exc

    def configure_alert_channels(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.alert_channels.save(value)
        except AlertChannelError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def delete_alert_channels(self) -> None:
        self.alert_channels.delete()

    def test_alert_channels(self) -> dict[str, Any]:
        try:
            return self.alert_channels.send(
                "Northstar ERP 测试通知",
                "外部通知渠道连接成功。此消息由管理员在 ERP 中手动触发。",
                event_id=f"test-{uuid4().hex}",
                test=True,
            )
        except AlertChannelError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def _dispatch_failure_alert(
        self, pipeline_id: str, stage: int, failures: list[str]
    ) -> None:
        if not failures:
            return
        try:
            status = self.alert_channels.public_status()
        except AlertChannelError:
            return
        if not status.get("enabled"):
            return
        rendered = "；".join(str(item)[:500] for item in failures[:5])
        event_id = hashlib.sha256(
            f"{pipeline_id}\0{stage}\0{rendered}".encode("utf-8")
        ).hexdigest()[:32]

        def deliver() -> None:
            try:
                self.alert_channels.send(
                    f"Northstar 门禁失败 · 商品 {pipeline_id} · 阶段 {stage}",
                    f"商品 ID：{pipeline_id}\n阶段：{stage}\n错误：{rendered}\n请登录 ERP 任务中心处理。",
                    event_id=event_id,
                )
            except AlertChannelError:
                pass

        threading.Thread(
            target=deliver,
            name=f"alert-{pipeline_id}-{stage}",
            daemon=True,
        ).start()

    def object_storage_status(self) -> dict[str, Any]:
        return self.object_storage.public_status()

    def configure_object_storage(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.object_storage.save(value)
        except ObjectStorageError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def delete_object_storage(self) -> None:
        self.object_storage.delete()

    def _mirror_generated_images(
        self, pipeline_id: str, inspection: Any
    ) -> list[dict[str, Any]]:
        client = self.object_storage.client()
        rows = [dict(item) for item in inspection if isinstance(item, Mapping)]
        if client is None:
            return rows
        cache: dict[str, dict[str, Any]] = {}
        for row in rows:
            source = str(row.get("final_public_url") or "").strip()
            if not source:
                continue
            row["upstream_public_url"] = source
            if source.startswith(f"{client.public_base_url}/"):
                row["storage_status"] = "ALREADY_MIRRORED"
                continue
            try:
                stored = cache.get(source)
                if stored is None:
                    stored = client.mirror(source, pipeline_id)
                    cache[source] = stored
                row["final_public_url"] = stored["public_url"]
                row["storage_status"] = "MIRRORED"
                row["storage_object_key"] = stored["object_key"]
                row["storage_sha256"] = stored["sha256"]
                row["storage_size_bytes"] = stored["size_bytes"]
                self.assets.upsert(
                    {
                        "object_key": stored["object_key"],
                        "public_url": stored["public_url"],
                        "source_url": source,
                        "sha256": stored["sha256"],
                        "content_type": stored.get("content_type"),
                        "size_bytes": stored["size_bytes"],
                        "asset_type": (
                            "ai_generated"
                            if str(row.get("plan_action") or "")
                            in {"reference_8", "sku_main", "ai_generate"}
                            else "translated"
                        ),
                        "storage_bucket": client.bucket,
                        "storage_endpoint": client.endpoint,
                    }
                )
                row["storage_bucket"] = client.bucket
                row["storage_endpoint"] = client.endpoint
            except (ObjectStorageError, AssetRegistryError, ValueError) as exc:
                row["final_public_url"] = None
                row["storage_status"] = "FAILED"
                row["error"] = f"图片固化到 OSS 失败：{str(exc)[:400]}"
        return rows

    def asset_library(self) -> dict[str, Any]:
        """Build a safe, cross-product view of source and managed image assets."""

        with self._lock:
            external: dict[str, dict[str, Any]] = {}
            managed: dict[str, dict[str, Any]] = {}
            public_base = str(
                self.object_storage.public_status().get("public_base_url") or ""
            ).rstrip("/")
            storage_status = self.object_storage.public_status()
            for pipeline in self.list_pipelines():
                pipeline_id = str(pipeline.get("pipeline_id") or "")
                if not pipeline_id:
                    continue
                stage5 = (pipeline.get("stages") or {}).get("5") or {}
                published = isinstance(stage5, Mapping) and stage5.get("status") == "PASS"
                if self.path(pipeline_id, 1).exists():
                    try:
                        raw = self.read_artifact(pipeline_id, 1)
                    except PipelineError:
                        raw = {}
                    for item in _image_items(raw):
                        url = str(item.get("url") or "").strip()
                        if not url:
                            continue
                        identifier = asset_id(url)
                        record = external.setdefault(
                            identifier,
                            {
                                "asset_id": identifier,
                                "public_url": url,
                                "source_url": url,
                                "asset_type": "source",
                                "managed": False,
                                "size_bytes": None,
                                "references": [],
                            },
                        )
                        record["references"].append(
                            {
                                "pipeline_id": pipeline_id,
                                "stage": 1,
                                "role": item.get("source_role"),
                                "sku_id": item.get("sku_id"),
                            }
                        )
                if not self.path(pipeline_id, 3).exists():
                    continue
                try:
                    localized = self.read_artifact(pipeline_id, 3).get("localized_images") or []
                except PipelineError:
                    continue
                for item in localized:
                    if not isinstance(item, Mapping):
                        continue
                    url = str(item.get("final_public_url") or "").strip()
                    if not url:
                        continue
                    object_key = str(item.get("storage_object_key") or "").strip().lstrip("/")
                    if not object_key and public_base and url.startswith(f"{public_base}/"):
                        object_key = url[len(public_base) + 1 :]
                    action = str(item.get("plan_action") or "")
                    kind = (
                        "ai_generated"
                        if action in {"reference_8", "sku_main", "ai_generate"}
                        else "translated"
                    )
                    reference = {
                        "pipeline_id": pipeline_id,
                        "stage": 3,
                        "role": item.get("source_role"),
                        "sku_id": item.get("sku_id"),
                        "module_number": item.get("module_number"),
                    }
                    if object_key:
                        identifier = managed_asset_id(object_key, url)
                        record = managed.setdefault(
                            identifier,
                            {
                                "asset_id": identifier,
                                "object_key": object_key,
                                "public_url": url,
                                "source_url": item.get("source_url"),
                                "asset_type": kind,
                                "managed": True,
                                "sha256": item.get("storage_sha256"),
                                "size_bytes": item.get("storage_size_bytes"),
                                "references": [],
                                "published": False,
                                "storage_bucket": (
                                    item.get("storage_bucket")
                                    or (
                                        storage_status.get("bucket")
                                        if public_base and url.startswith(f"{public_base}/")
                                        else None
                                    )
                                ),
                                "storage_endpoint": (
                                    item.get("storage_endpoint")
                                    or (
                                        storage_status.get("endpoint")
                                        if public_base and url.startswith(f"{public_base}/")
                                        else None
                                    )
                                ),
                            },
                        )
                        record["references"].append(reference)
                        record["published"] = bool(record.get("published") or published)
                    else:
                        identifier = asset_id(url)
                        record = external.setdefault(
                            identifier,
                            {
                                "asset_id": identifier,
                                "public_url": url,
                                "source_url": item.get("source_url"),
                                "asset_type": kind,
                                "managed": False,
                                "size_bytes": None,
                                "references": [],
                            },
                        )
                        record["references"].append(reference)

            managed_rows = self.assets.synchronize(managed)
            external_rows = []
            for record in external.values():
                references = list(record.get("references") or [])
                external_rows.append(
                    {
                        **record,
                        "reference_count": len(references),
                        "orphaned": False,
                        "published": False,
                        "can_delete": False,
                    }
                )
            for record in managed_rows:
                record["can_delete"] = bool(
                    record.get("orphaned") and not record.get("published")
                )
            rows = managed_rows + external_rows
            rows.sort(
                key=lambda row: (
                    not bool(row.get("orphaned")),
                    str(row.get("asset_type") or ""),
                    str(row.get("asset_id") or ""),
                )
            )
            bytes_used = sum(int(row.get("size_bytes") or 0) for row in managed_rows)
            orphaned_bytes = sum(
                int(row.get("size_bytes") or 0)
                for row in managed_rows
                if row.get("orphaned")
            )
            return {
                "schema_version": "1.0",
                "summary": {
                    "total": len(rows),
                    "managed": len(managed_rows),
                    "source": len(external_rows),
                    "orphaned": sum(1 for row in managed_rows if row.get("orphaned")),
                    "published": sum(1 for row in managed_rows if row.get("published")),
                    "managed_bytes": bytes_used,
                    "orphaned_bytes": orphaned_bytes,
                },
                "assets": rows,
                "generated_at": _now(),
            }

    def delete_asset(self, identifier: Any) -> dict[str, Any]:
        identifier = str(identifier or "").strip()
        inventory = self.asset_library()
        record = next(
            (item for item in inventory["assets"] if item.get("asset_id") == identifier),
            None,
        )
        if not record or not record.get("managed"):
            raise PipelineError("只能删除由本系统托管的 OSS 图片资产", status=404)
        if record.get("published"):
            raise PipelineError("该图片曾进入成功上架记录，为防止在售商品丢图，禁止删除", status=409)
        if int(record.get("reference_count") or 0) > 0:
            raise PipelineError("该图片仍被商品任务引用，禁止删除", status=409)
        client = self.object_storage.client()
        if client is None:
            raise PipelineError("请先配置阿里云 OSS 图片存储", status=409)
        if record.get("storage_bucket") and record.get("storage_bucket") != client.bucket:
            raise PipelineError("该图片属于历史 OSS Bucket，当前凭证不匹配，已禁止删除", status=409)
        if record.get("storage_endpoint") and record.get("storage_endpoint") != client.endpoint:
            raise PipelineError("该图片属于历史 OSS Endpoint，当前凭证不匹配，已禁止删除", status=409)
        try:
            client.delete(str(record.get("object_key") or ""))
            self.assets.delete(identifier)
        except (ObjectStorageError, AssetRegistryError) as exc:
            raise PipelineError(str(exc), status=422) from exc
        return self.asset_library()

    def _protect_published_assets(self, pipeline_id: str) -> None:
        """Persist a sticky protection bit as soon as Ozon verifies publication."""

        if not self.path(pipeline_id, 3).exists():
            return
        final = self.read_artifact(pipeline_id, 3)
        for item in final.get("localized_images") or []:
            if not isinstance(item, Mapping):
                continue
            object_key = str(item.get("storage_object_key") or "").strip()
            if not object_key:
                continue
            self.assets.upsert(
                {
                    "object_key": object_key,
                    "public_url": item.get("final_public_url"),
                    "source_url": item.get("source_url"),
                    "sha256": item.get("storage_sha256"),
                    "size_bytes": item.get("storage_size_bytes"),
                    "asset_type": (
                        "ai_generated"
                        if str(item.get("plan_action") or "")
                        in {"reference_8", "sku_main", "ai_generate"}
                        else "translated"
                    ),
                    "published": True,
                    "storage_bucket": item.get("storage_bucket"),
                    "storage_endpoint": item.get("storage_endpoint"),
                }
            )

    def record_activity(
        self,
        *,
        actor: str,
        method: str,
        path: str,
        status: int,
        remote_address: str = "",
    ) -> None:
        self.activity.append(
            actor=actor,
            method=method,
            path=path,
            status=status,
            remote_address=remote_address,
        )

    def activity_log(self, limit: Any = 100) -> dict[str, Any]:
        try:
            normalized_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise PipelineError("审计记录数量必须是整数") from exc
        return {
            "schema_version": "1.0",
            "activities": self.activity.list(normalized_limit),
        }

    def configure_ai_provider(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.ai_provider.save(value)
        except AiProviderConfigurationError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def delete_ai_provider(self) -> None:
        self.ai_provider.delete()

    def list_stores(self) -> list[dict[str, Any]]:
        return self.stores.list_public()

    def finance_settings(self, store_id: str) -> dict[str, Any]:
        if not any(item["store_id"] == store_id for item in self.list_stores()):
            raise PipelineError("未找到对应的 Ozon 店铺", status=404)
        return self.finance.get(store_id)

    def save_finance_settings(self, store_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if not any(item["store_id"] == store_id for item in self.list_stores()):
            raise PipelineError("未找到对应的 Ozon 店铺", status=404)
        try:
            return self.finance.save(store_id, value)
        except FinanceSettingsError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def pricing_calculation(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return calculate_pricing(value)
        except PricingError as exc:
            raise PipelineError(str(exc), status=422) from exc

    def _product_cost_index(self, store_id: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for pipeline in self.list_pipelines():
            pipeline_id = str(pipeline.get("pipeline_id") or "")
            if not pipeline_id or not self.path(pipeline_id, 1).exists() or not self.path(pipeline_id, 4).exists():
                continue
            try:
                source = self.read_artifact(pipeline_id, 1)
                payload = self.read_artifact(pipeline_id, 4)
            except PipelineError:
                continue
            sku_rows = [item for item in source.get("sku_list") or [] if isinstance(item, Mapping)] or [{}]
            items = [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
            for sku, item in zip(sku_rows, items):
                offer_id = str(item.get("offer_id") or "")
                if not offer_id.startswith(f"{store_id}-"):
                    continue
                try:
                    cost = Decimal(str(sku.get("purchase_price_cny")))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if cost <= 0:
                    continue
                unit = str(item.get("dimension_unit") or "mm").lower()
                length_factor = Decimal("0.1") if unit == "mm" else Decimal("1")
                weight_unit = str(item.get("weight_unit") or "g").lower()
                weight_factor = Decimal("1000") if weight_unit == "kg" else Decimal("1")
                try:
                    length = Decimal(str(item.get("depth") or 0)) * length_factor
                    width = Decimal(str(item.get("width") or 0)) * length_factor
                    height = Decimal(str(item.get("height") or 0)) * length_factor
                    weight = Decimal(str(item.get("weight") or 0)) * weight_factor
                except (InvalidOperation, TypeError, ValueError):
                    continue
                result[offer_id] = {
                    "purchase_cost_cny": cost,
                    "length_cm": length,
                    "width_cm": width,
                    "height_cm": height,
                    "weight_g": weight,
                }
        return result

    def _purchase_cost_index(self, store_id: str) -> dict[str, Decimal]:
        return {
            offer_id: item["purchase_cost_cny"]
            for offer_id, item in self._product_cost_index(store_id).items()
        }

    def operations_dashboard(self, store_id: str = "", days: int = 30) -> dict[str, Any]:
        try:
            days = max(1, min(90, int(days)))
        except (TypeError, ValueError) as exc:
            raise PipelineError("订单时间范围必须是 1–90 天") from exc
        stores = self.list_stores()
        selected_id = str(store_id or (stores[0]["store_id"] if len(stores) == 1 else "")).strip()
        if not selected_id:
            raise PipelineError("请选择要读取订单与库存的 Ozon 店铺", status=409)
        try:
            credentials = self.stores.credentials(selected_id)
        except StoreAuthorizationError as exc:
            raise PipelineError(str(exc), status=404) from exc
        client = OzonOperationsClient(credentials["client_id"], credentials["api_key"])
        orders, warnings = client.orders(days=days)
        try:
            stocks = client.stocks()
        except OzonOperationsError as exc:
            stocks = []
            warnings.append(f"库存读取失败：{exc}")
        low_stock_threshold = max(0, int(os.environ.get("ERP_LOW_STOCK_THRESHOLD") or "5"))
        low_stocks = [item for item in stocks if int(item.get("available") or 0) <= low_stock_threshold]
        exception_statuses = {"cancelled", "canceled", "arbitration", "client_arbitration", "not_accepted"}
        exceptions = [
            item
            for item in orders
            if str(item.get("status") or "").lower() in exception_statuses
        ]
        commercial_orders = [
            order
            for order in orders
            if str(order.get("status") or "").lower() not in {"cancelled", "canceled"}
        ]
        revenue_by_currency: dict[str, Decimal] = {}
        for order in commercial_orders:
            currency = str(order.get("currency_code") or "RUB")
            try:
                amount = Decimal(str(order.get("revenue") or "0"))
            except InvalidOperation:
                amount = Decimal("0")
            revenue_by_currency[currency] = revenue_by_currency.get(currency, Decimal("0")) + amount
        settings = self.finance.get(selected_id)
        product_cost_index = self._product_cost_index(selected_id)
        cost_index = {
            offer_id: item["purchase_cost_cny"]
            for offer_id, item in product_cost_index.items()
        }
        units_by_offer: dict[str, int] = {}
        purchase_cost_cny = Decimal("0")
        total_cost_cny = Decimal("0")
        estimated_profit_cny = Decimal("0")
        missing_costs: set[str] = set()
        pricing_failures: list[str] = []
        for order in commercial_orders:
            for product in order.get("products") or []:
                if not isinstance(product, Mapping):
                    continue
                offer_id = str(product.get("offer_id") or "")
                quantity = int(product.get("quantity") or 0)
                if quantity <= 0:
                    continue
                units_by_offer[offer_id] = units_by_offer.get(offer_id, 0) + quantity
                if offer_id in {"", "—"}:
                    continue
                economics = product_cost_index.get(offer_id)
                if economics is None:
                    missing_costs.add(offer_id)
                    continue
                purchase_cost_cny += economics["purchase_cost_cny"] * quantity
                currency = str(product.get("currency_code") or order.get("currency_code") or "RUB")
                try:
                    raw_price = Decimal(str(product.get("price") or "0"))
                except InvalidOperation:
                    pricing_failures.append(f"{offer_id} 的销售价无效")
                    continue
                exchange = Decimal(str(settings["cny_to_rub_rate"]))
                if currency == "RUB":
                    sale_cny = raw_price / exchange
                elif currency == "CNY":
                    sale_cny = raw_price
                else:
                    pricing_failures.append(f"{offer_id} 的币种 {currency} 暂不支持")
                    continue
                try:
                    result = calculate_pricing(
                        {
                            **settings,
                            **economics,
                            "sale_price_cny": sale_cny,
                        }
                    )
                except PricingError as exc:
                    pricing_failures.append(f"{offer_id}：{exc}")
                    continue
                total_cost_cny += Decimal(result["total_cost_cny"]) * quantity
                estimated_profit_cny += Decimal(result["unit_profit_cny"]) * quantity
        procurement: list[dict[str, Any]] = []
        stock_by_offer = {
            str(item.get("offer_id") or ""): item
            for item in stocks
            if str(item.get("offer_id") or "") not in {"", "—"}
        }
        target_days = int(settings["lead_time_days"]) + int(settings["safety_stock_days"])
        for offer_id, stock in stock_by_offer.items():
            sold = units_by_offer.get(offer_id, 0)
            daily_sales = sold / days
            target_stock = max(low_stock_threshold * 2, math.ceil(daily_sales * target_days))
            reorder_quantity = max(0, target_stock - int(stock.get("available") or 0))
            if reorder_quantity <= 0:
                continue
            unit_cost = cost_index.get(offer_id)
            procurement.append(
                {
                    "offer_id": offer_id,
                    "available": stock.get("available"),
                    "sold_units": sold,
                    "daily_sales": round(daily_sales, 2),
                    "target_stock": target_stock,
                    "reorder_quantity": reorder_quantity,
                    "unit_cost_cny": str(unit_cost) if unit_cost is not None else None,
                    "purchase_total_cny": str((unit_cost * reorder_quantity).quantize(Decimal("0.01"))) if unit_cost is not None else None,
                }
            )
        procurement.sort(key=lambda item: int(item["reorder_quantity"]), reverse=True)
        final_statuses = {"delivered", "cancelled", "canceled", "delivering"}
        logistics_alerts: list[dict[str, Any]] = []
        current = datetime.now(timezone.utc)
        for order in orders:
            shipment = str(order.get("shipment_date") or "")
            if not shipment or str(order.get("status") or "").lower() in final_statuses:
                continue
            try:
                deadline = datetime.fromisoformat(shipment.replace("Z", "+00:00"))
            except ValueError:
                continue
            hours_left = (deadline - current).total_seconds() / 3600
            if hours_left <= 24:
                logistics_alerts.append({**order, "hours_left": round(hours_left, 1), "severity": "overdue" if hours_left < 0 else "urgent"})
        profit_status = "READY"
        profit_reason = ""
        if not settings["configured"]:
            profit_status, profit_reason = "NEEDS_CONFIGURATION", "请先填写表格中的佣金、退货、提现、广告、尾程、物流和汇率参数"
        elif missing_costs:
            profit_status, profit_reason = "NEEDS_COST", "部分订单商品缺少人民币采购成本"
        elif pricing_failures:
            profit_status, profit_reason = "NEEDS_INPUT", pricing_failures[0]
        return {
            "schema_version": "1.0",
            "store_id": selected_id,
            "store_name": credentials.get("name"),
            "period_days": days,
            "low_stock_threshold": low_stock_threshold,
            "summary": {
                "orders": len(orders),
                "exceptions": len(exceptions),
                "stock_skus": len(stocks),
                "low_stock": len(low_stocks),
                "revenue": {key: str(value.quantize(Decimal("0.01"))) for key, value in revenue_by_currency.items()},
            },
            "orders": orders,
            "exceptions": exceptions,
            "stocks": stocks,
            "low_stocks": low_stocks,
            "procurement": procurement,
            "logistics_alerts": logistics_alerts,
            "profit": {
                "status": profit_status,
                "reason": profit_reason,
                "estimated_profit_cny": str(estimated_profit_cny.quantize(Decimal("0.01"))) if profit_status == "READY" else None,
                "total_cost_cny": str(total_cost_cny.quantize(Decimal("0.01"))),
                "purchase_cost_cny": str(purchase_cost_cny.quantize(Decimal("0.01"))),
                "missing_cost_offer_ids": sorted(missing_costs),
                "pricing_failures": pricing_failures,
                "settings": settings,
            },
            "warnings": warnings,
            "refreshed_at": _now(),
        }

    def commercial_status(self, username: str = "") -> dict[str, Any]:
        """Return the current workspace identity, plan and metered usage."""

        plan_code = str(os.environ.get("ERP_PLAN") or "business").strip().lower()
        if plan_code not in PLAN_CATALOG:
            plan_code = "business"
        plan = PLAN_CATALOG[plan_code]
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        monthly_pipelines = [
            item
            for item in self.list_pipelines()
            if str(item.get("created_at") or item.get("updated_at") or "").startswith(month)
        ]
        ai_images = 0
        for item in monthly_pipelines:
            pipeline_id = item.get("pipeline_id")
            if not pipeline_id or not self.path(str(pipeline_id), 3).exists():
                continue
            try:
                localized = self.read_artifact(str(pipeline_id), 3).get("localized_images") or []
            except PipelineError:
                continue
            for image in localized:
                if not isinstance(image, Mapping):
                    continue
                action = str(image.get("plan_action") or image.get("action") or "")
                if action in {"reference_8", "sku_main", "ai_generate"}:
                    ai_images += 1
        try:
            identity = self.team.identity(username)
            team = self.team.list_public(username)
        except TeamError as exc:
            raise PipelineError(str(exc), status=exc.status) from exc
        limits = dict(plan["limits"])
        usage = {
            "products": len(monthly_pipelines),
            "stores": len(self.list_stores()),
            "ai_images": ai_images,
            "members": len(team["members"]),
        }
        return {
            "schema_version": "1.0",
            "workspace": {"name": "Northstar Workspace", "id": "default"},
            "identity": {
                **identity,
                "permission_labels": [
                    label
                    for permission, label in {
                        "pipeline.create": "商品管理",
                        "stores.manage": "店铺授权",
                        "listing.publish": "真实上架",
                        "team.manage": "团队管理",
                        "billing.view": "账单管理",
                        "images.manage": "图片设计",
                        "storage.manage": "图片存储管理",
                        "audit.read": "操作审计",
                    }.items()
                    if permission in identity["permissions"]
                ],
            },
            "plan": {
                "code": plan_code,
                "name": plan["name"],
                "monthly_price_cny": plan["monthly_price_cny"],
                "limits": limits,
            },
            "usage": usage,
            "period": month,
        }

    def require_permission(self, username: str, permission: str) -> dict[str, Any]:
        try:
            return self.team.require(username, permission)
        except TeamError as exc:
            raise PipelineError(str(exc), status=exc.status) from exc

    def team_status(self, username: str) -> dict[str, Any]:
        try:
            return self.team.list_public(username)
        except TeamError as exc:
            raise PipelineError(str(exc), status=exc.status) from exc

    def save_team_member(self, username: str, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            team = self.team.list_public(username)
            target = str(value.get("username") or "").strip().casefold()
            exists = any(
                str(item.get("username") or "").casefold() == target
                for item in team["members"]
            )
            account = self.commercial_status(username)
            if not exists and len(team["members"]) >= int(account["plan"]["limits"]["members"]):
                raise PipelineError(
                    f"团队成员额度已用完（{len(team['members'])}/{account['plan']['limits']['members']}），请升级套餐后继续",
                    status=402,
                )
            return self.team.save_member(username, value)
        except TeamError as exc:
            raise PipelineError(str(exc), status=exc.status) from exc

    def delete_team_member(self, username: str, target: str) -> None:
        try:
            self.team.delete_member(username, target)
        except TeamError as exc:
            raise PipelineError(str(exc), status=exc.status) from exc

    def ensure_product_quota(self) -> None:
        plan_code = str(os.environ.get("ERP_PLAN") or "business").strip().lower()
        plan = PLAN_CATALOG.get(plan_code, PLAN_CATALOG["business"])
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        used = sum(
            1
            for item in self.list_pipelines()
            if str(item.get("created_at") or item.get("updated_at") or "").startswith(month)
        )
        limit = int(plan["limits"]["products"])
        if used >= limit:
            raise PipelineError(
                f"本月商品任务额度已用完（{used}/{limit}），请升级套餐后继续",
                status=402,
            )

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

    def _product_settings_path(self, pipeline_id: Any) -> Path:
        return self.directory(pipeline_id) / PRODUCT_SETTINGS_FILE

    def _product_versions_path(self, pipeline_id: Any) -> Path:
        return self.directory(pipeline_id) / PRODUCT_VERSIONS_FILE

    @staticmethod
    def _public_product_settings(value: Mapping[str, Any]) -> dict[str, Any]:
        dimensions = value.get("dimension_defaults") or {}
        if not isinstance(dimensions, Mapping):
            dimensions = {}
        return {
            "store_id": str(value.get("store_id") or ""),
            "cny_to_rub_rate": str(value.get("cny_to_rub_rate") or ""),
            "vat": str(value.get("vat") or "0"),
            "dimension_defaults": {
                "depth": dimensions.get("depth"),
                "width": dimensions.get("width"),
                "height": dimensions.get("height"),
                "weight": dimensions.get("weight"),
                "dimension_unit": str(dimensions.get("dimension_unit") or "mm"),
                "weight_unit": str(dimensions.get("weight_unit") or "g"),
            },
        }

    def product_settings(self, pipeline_id: Any) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        if not self._state_path(pipeline_id).exists():
            raise PipelineError("商品工作区不存在", status=404)
        path = self._product_settings_path(pipeline_id)
        if path.exists():
            return self._public_product_settings(_read_object(path))
        context_path = self.directory(pipeline_id) / "04_context.json"
        if context_path.exists():
            context = _read_object(context_path)
            store_config = context.get("store_config") or {}
            if isinstance(store_config, Mapping):
                return self._public_product_settings(store_config)
        state = _read_object(self._state_path(pipeline_id))
        return self._public_product_settings(
            {
                "store_id": state.get("store_id"),
                "cny_to_rub_rate": "",
                "vat": "0",
                "dimension_defaults": {},
            }
        )

    def _validate_product_settings_patch(
        self, value: Any
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or not value:
            raise PipelineError("请至少选择一个需要批量修改的字段")
        allowed = {"store_id", "cny_to_rub_rate", "vat", "dimension_defaults"}
        unknown = sorted(set(map(str, value)) - allowed)
        if unknown:
            raise PipelineError(f"批量编辑包含不允许的字段：{', '.join(unknown)}")
        patch: dict[str, Any] = {}
        if "store_id" in value:
            store_id = str(value.get("store_id") or "").strip()
            if not store_id:
                raise PipelineError("目标店铺不能为空")
            self._selected_store("", store_id)
            patch["store_id"] = store_id
        if "cny_to_rub_rate" in value:
            try:
                rate = Decimal(str(value.get("cny_to_rub_rate") or "").strip())
            except (InvalidOperation, ValueError) as exc:
                raise PipelineError("汇率必须是有效数字") from exc
            if not rate.is_finite() or rate <= 0:
                raise PipelineError("汇率必须大于 0")
            patch["cny_to_rub_rate"] = str(rate)
        if "vat" in value:
            vat = str(value.get("vat") or "0").strip()
            if vat not in {"0", "0.05", "0.07", "0.1", "0.12", "0.2"}:
                raise PipelineError("VAT 仅支持 Ozon 合法编码 0、0.05、0.07、0.1、0.12 或 0.2")
            patch["vat"] = vat
        if "dimension_defaults" in value:
            dimensions = value.get("dimension_defaults")
            if not isinstance(dimensions, Mapping):
                raise PipelineError("默认尺寸重量必须是对象")
            normalized: dict[str, Any] = {}
            for key in ("depth", "width", "height", "weight"):
                raw = dimensions.get(key)
                try:
                    number = Decimal(str(raw))
                except (InvalidOperation, ValueError) as exc:
                    raise PipelineError("默认尺寸和重量必须是有效数字") from exc
                if not number.is_finite() or number <= 0 or number != number.to_integral_value():
                    raise PipelineError("默认尺寸和重量必须是正整数")
                normalized[key] = int(number)
            dimension_unit = str(dimensions.get("dimension_unit") or "mm").lower()
            weight_unit = str(dimensions.get("weight_unit") or "g").lower()
            if dimension_unit not in {"mm", "cm"} or weight_unit not in {"g", "kg"}:
                raise PipelineError("尺寸单位仅支持 mm/cm，重量单位仅支持 g/kg")
            normalized["dimension_unit"] = dimension_unit
            normalized["weight_unit"] = weight_unit
            patch["dimension_defaults"] = normalized
        return patch

    def _product_versions_document(self, pipeline_id: str) -> dict[str, Any]:
        path = self._product_versions_path(pipeline_id)
        if not path.exists():
            return {"schema_version": "1.0", "versions": []}
        document = _read_object(path)
        if not isinstance(document.get("versions"), list):
            raise PipelineError("商品版本历史格式无效", status=500)
        return document

    def _append_product_version(
        self,
        pipeline_id: str,
        settings: Mapping[str, Any],
        *,
        actor: str,
        note: str,
        source: str,
    ) -> dict[str, Any]:
        document = self._product_versions_document(pipeline_id)
        version = {
            "version_id": uuid4().hex[:24],
            "created_at": _now(),
            "actor": str(actor or "system")[:64],
            "note": str(note or "")[:200],
            "source": source,
            "settings": self._public_product_settings(settings),
        }
        document["versions"] = [*document["versions"], version][-100:]
        _atomic_json(self._product_versions_path(pipeline_id), document)
        return version

    def _apply_product_settings(
        self,
        pipeline_id: str,
        settings: Mapping[str, Any],
        *,
        actor: str,
        note: str,
        source: str,
    ) -> dict[str, Any]:
        normalized = self._public_product_settings(settings)
        current = self.product_settings(pipeline_id)
        if normalized == current:
            return {"changed": False, "settings": current, "version": None}
        history = self._product_versions_document(pipeline_id)
        if not history["versions"]:
            self._append_product_version(
                pipeline_id,
                current,
                actor=actor,
                note="批量编辑前的初始版本",
                source="BASELINE",
            )
        _atomic_json(
            self._product_settings_path(pipeline_id),
            {
                "schema_version": "1.0",
                **normalized,
                "updated_at": _now(),
                "updated_by": str(actor or "system")[:64],
            },
        )
        version = self._append_product_version(
            pipeline_id,
            normalized,
            actor=actor,
            note=note,
            source=source,
        )
        state_path = self._state_path(pipeline_id)
        state = _read_object(state_path)
        if normalized.get("store_id"):
            state["store_id"] = normalized["store_id"]
        for stage_number in (4, 5):
            stage = (state.get("stages") or {}).get(str(stage_number))
            if isinstance(stage, dict):
                stage["status"] = "STALE"
                stage["stale_after_product_edit"] = True
                stage["checked_at"] = _now()
        state["updated_at"] = _now()
        _atomic_json(state_path, state)
        return {"changed": True, "settings": normalized, "version": version}

    def batch_edit_products(
        self, value: Mapping[str, Any], actor: str = ""
    ) -> dict[str, Any]:
        requested = value.get("pipeline_ids") or []
        if not isinstance(requested, list):
            raise PipelineError("pipeline_ids 必须是数组")
        pipeline_ids = list(dict.fromkeys(_safe_pipeline_id(item) for item in requested))
        if not pipeline_ids or len(pipeline_ids) > _MAX_BATCH_PROCESS_ITEMS:
            raise PipelineError("批量编辑每次必须选择 1–50 个商品")
        patch = self._validate_product_settings_patch(value.get("patch"))
        note = str(value.get("note") or "批量编辑商品上架参数").strip()[:200]
        results: list[dict[str, Any]] = []
        with self._lock:
            for pipeline_id in pipeline_ids:
                if not self._state_path(pipeline_id).exists():
                    results.append(
                        {"pipeline_id": pipeline_id, "status": "FAIL", "error": "商品工作区不存在"}
                    )
                    continue
                try:
                    current = self.product_settings(pipeline_id)
                    merged = {**current, **patch}
                    outcome = self._apply_product_settings(
                        pipeline_id,
                        merged,
                        actor=actor,
                        note=note,
                        source="BATCH_EDIT",
                    )
                    results.append(
                        {
                            "pipeline_id": pipeline_id,
                            "status": "UPDATED" if outcome["changed"] else "UNCHANGED",
                            "version_id": (outcome.get("version") or {}).get("version_id"),
                        }
                    )
                except PipelineError as exc:
                    results.append(
                        {"pipeline_id": pipeline_id, "status": "FAIL", "error": str(exc)}
                    )
        return {
            "schema_version": "1.0",
            "total": len(results),
            "updated": sum(1 for item in results if item["status"] == "UPDATED"),
            "unchanged": sum(1 for item in results if item["status"] == "UNCHANGED"),
            "failed": sum(1 for item in results if item["status"] == "FAIL"),
            "results": results,
        }

    def product_versions(self, pipeline_id: Any) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        if not self._state_path(pipeline_id).exists():
            raise PipelineError("商品工作区不存在", status=404)
        document = self._product_versions_document(pipeline_id)
        return {
            "schema_version": "1.0",
            "pipeline_id": pipeline_id,
            "current": self.product_settings(pipeline_id),
            "versions": list(reversed(document["versions"])),
        }

    def restore_product_version(
        self, pipeline_id: Any, version_id: Any, actor: str = ""
    ) -> dict[str, Any]:
        pipeline_id = _safe_pipeline_id(pipeline_id)
        version_id = str(version_id or "").strip()
        with self._lock:
            versions = self._product_versions_document(pipeline_id)["versions"]
            selected = next(
                (item for item in versions if item.get("version_id") == version_id),
                None,
            )
            if not isinstance(selected, Mapping):
                raise PipelineError("商品版本不存在", status=404)
            settings = selected.get("settings")
            if not isinstance(settings, Mapping):
                raise PipelineError("商品版本内容无效", status=409)
            outcome = self._apply_product_settings(
                pipeline_id,
                settings,
                actor=actor,
                note=f"回滚至版本 {version_id}",
                source="ROLLBACK",
            )
        return {
            "pipeline_id": pipeline_id,
            "restored_version_id": version_id,
            **outcome,
        }

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
        if not passed:
            self._dispatch_failure_alert(pipeline_id, stage, failures)

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

    @staticmethod
    def _failure_diagnosis(message: str) -> tuple[str, str]:
        text = message.lower()
        if any(key in text for key in ("图片", "image", "photo", "rich content", "富内容")):
            return "MEDIA", "检查图片格式、公共访问地址和 Ozon 托管状态后重试"
        if any(key in text for key in ("类目", "category", "type_id", "商品类型")):
            return "CATEGORY", "重新搜索并人工确认 Ozon 官方类目与商品类型"
        if any(key in text for key in ("属性", "attribute", "dictionary", "特征")):
            return "ATTRIBUTE", "打开第四阶段，补充或更正对应的 Ozon 特征值"
        if any(key in text for key in ("价格", "price", "cny", "rub", "汇率")):
            return "PRICING", "核对 SKU 采购价、币种和汇率，确保售价大于零"
        if any(key in text for key in ("ai ", "模型", "中转站", "relay")):
            return "AI", "验证 AI 模型名称、密钥余额和服务通道后从第三阶段续跑"
        if any(key in text for key in ("http", "api", "task_id", "ozon")):
            return "API", "查看 Ozon 返回详情；确认接口版本、凭证、限流和任务状态"
        return "VALIDATION", "查看阶段产物中的门禁详情，修正数据后只重跑失败阶段"

    def dashboard(self) -> dict[str, Any]:
        """Return commercial workbench counters and actionable failures."""

        pipelines = self.list_pipelines()
        counters = {
            "total": len(pipelines),
            "completed": 0,
            "running": 0,
            "failed": 0,
            "draft": 0,
        }
        issues: list[dict[str, Any]] = []
        for pipeline in pipelines:
            stage_rows = pipeline.get("stages") or {}
            statuses = [
                str(stage.get("status") or "LOCKED")
                for stage in stage_rows.values()
                if isinstance(stage, Mapping)
            ]
            if sum(1 for status in statuses if status == "PASS") == 5:
                counters["completed"] += 1
            elif "RUNNING" in statuses:
                counters["running"] += 1
            elif "FAIL" in statuses:
                counters["failed"] += 1
            else:
                counters["draft"] += 1
            for stage_number, stage in stage_rows.items():
                if not isinstance(stage, Mapping) or stage.get("status") != "FAIL":
                    continue
                failures = [str(item) for item in stage.get("failures") or [] if item]
                for failure in failures[:5]:
                    category, suggestion = self._failure_diagnosis(failure)
                    issues.append(
                        {
                            "pipeline_id": pipeline.get("pipeline_id"),
                            "stage": int(stage_number),
                            "category": category,
                            "message": failure[:1000],
                            "suggestion": suggestion,
                            "checked_at": stage.get("checked_at"),
                        }
                    )
        issues.sort(key=lambda item: str(item.get("checked_at") or ""), reverse=True)
        return {
            "schema_version": "1.0",
            "counters": counters,
            "open_issue_count": len(issues),
            "issues": issues[:100],
            "generated_at": _now(),
        }

    def product_catalog(self) -> dict[str, Any]:
        """Return a safe read model for the commercial product center."""

        products: list[dict[str, Any]] = []
        summary = {
            "total": 0,
            "published": 0,
            "ready": 0,
            "failed": 0,
            "processing": 0,
            "draft": 0,
            "needs_update": 0,
            "sku_count": 0,
        }
        for pipeline in self.list_pipelines():
            pipeline_id = str(pipeline.get("pipeline_id") or "")
            if not pipeline_id:
                continue

            def artifact(stage: int) -> dict[str, Any]:
                if not self.path(pipeline_id, stage).exists():
                    return {}
                try:
                    return self.read_artifact(pipeline_id, stage)
                except PipelineError:
                    return {}

            raw = artifact(1)
            mapped = artifact(2)
            final = artifact(3)
            payload = artifact(4)
            payload_items = [
                item
                for item in payload.get("items") or []
                if isinstance(item, Mapping)
            ]
            sku_rows = [
                item
                for item in raw.get("sku_list") or []
                if isinstance(item, Mapping)
            ]
            sku_count = len(sku_rows) or len(payload_items)
            title_zh = str(raw.get("title_zh") or "").strip()
            title_ru = str(final.get("title_ru") or "").strip()
            if not title_ru and payload_items:
                title_ru = str(payload_items[0].get("name") or "").strip()

            thumbnail_url = ""
            for image in final.get("localized_images") or []:
                if isinstance(image, Mapping) and image.get("final_public_url"):
                    thumbnail_url = str(image["final_public_url"])
                    break
            if not thumbnail_url:
                images = _image_items(raw)
                if images:
                    thumbnail_url = str(images[0].get("url") or "")

            prices: list[Decimal] = []
            currencies: set[str] = set()
            offer_ids: list[str] = []
            for item in payload_items:
                offer_id = str(item.get("offer_id") or "").strip()
                if offer_id:
                    offer_ids.append(offer_id)
                currency = str(item.get("currency_code") or "").strip().upper()
                if currency:
                    currencies.add(currency)
                try:
                    price = Decimal(str(item.get("price") or ""))
                except InvalidOperation:
                    continue
                if price.is_finite() and price > 0:
                    prices.append(price)

            stages = pipeline.get("stages") or {}
            statuses = {
                str(number): str(stage.get("status") or "LOCKED")
                for number, stage in stages.items()
                if isinstance(stage, Mapping)
            }
            passed_count = sum(1 for status in statuses.values() if status == "PASS")
            current_stage = next(
                (
                    number
                    for number in range(1, 6)
                    if statuses.get(str(number)) in {"FAIL", "RUNNING", "STALE"}
                    or statuses.get(str(number), "LOCKED") != "PASS"
                ),
                5,
            )
            latest_failure = ""
            for number in range(5, 0, -1):
                stage = stages.get(str(number))
                if not isinstance(stage, Mapping) or stage.get("status") != "FAIL":
                    continue
                latest_failure = str(next(iter(stage.get("failures") or []), ""))[:500]
                current_stage = number
                break

            if "RUNNING" in statuses.values():
                status = "processing"
            elif "FAIL" in statuses.values():
                status = "failed"
            elif statuses.get("5") == "PASS":
                status = "published"
            elif statuses.get("5") == "STALE":
                status = "needs_update"
            elif statuses.get("4") == "PASS":
                status = "ready"
            else:
                status = "draft"

            row = {
                "pipeline_id": pipeline_id,
                "title_zh": title_zh,
                "title_ru": title_ru,
                "display_title": title_ru or title_zh or f"1688 商品 {pipeline_id}",
                "thumbnail_url": thumbnail_url,
                "status": status,
                "stage_passed_count": passed_count,
                "current_stage": current_stage,
                "latest_failure": latest_failure,
                "sku_count": sku_count,
                "offer_ids": offer_ids,
                "store_id": pipeline.get("store_id"),
                "ozon_category_id": (
                    final.get("ozon_category_id") or mapped.get("ozon_category_id")
                ),
                "ozon_type_id": final.get("ozon_type_id") or mapped.get("ozon_type_id"),
                "price_min": str(min(prices)) if prices else None,
                "price_max": str(max(prices)) if prices else None,
                "currency_code": next(iter(currencies)) if len(currencies) == 1 else None,
                "updated_at": pipeline.get("updated_at"),
                "created_at": pipeline.get("created_at"),
                "product_version_count": len(
                    self._product_versions_document(pipeline_id)["versions"]
                ),
            }
            products.append(row)
            summary[status] += 1
            summary["sku_count"] += sku_count

        products.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        summary["total"] = len(products)
        return {
            "schema_version": "1.0",
            "summary": summary,
            "products": products,
            "generated_at": _now(),
        }

    def notification_center(self, username: Any) -> dict[str, Any]:
        """Build actionable notifications from current failures and personal read state."""

        identity = self.team.require(username, "workspace.read")
        items: list[dict[str, Any]] = []
        for issue in self.dashboard()["issues"]:
            pipeline_id = str(issue.get("pipeline_id") or "")
            stage = int(issue.get("stage") or 0)
            message = str(issue.get("message") or "")
            digest = hashlib.sha256(
                f"{pipeline_id}\0{stage}\0{message}".encode("utf-8")
            ).hexdigest()[:24]
            items.append(
                {
                    "notification_id": digest,
                    "severity": "ERROR",
                    "title": f"商品 {pipeline_id} · 阶段 {stage} 门禁失败",
                    "message": message[:1000],
                    "suggestion": issue.get("suggestion"),
                    "pipeline_id": pipeline_id,
                    "stage": stage,
                    "created_at": issue.get("checked_at"),
                    "requires_manual_confirmation": stage == 5,
                }
            )
        decorated = self.notifications.decorate(identity["username"], items)
        return {
            "schema_version": "1.0",
            "notifications": decorated,
            "unread_count": sum(1 for item in decorated if not item["read"]),
            "generated_at": _now(),
        }

    def mark_notifications_read(
        self, username: Any, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        identity = self.team.require(username, "workspace.read")
        current = self.notification_center(identity["username"])
        current_ids = {
            str(item["notification_id"]) for item in current["notifications"]
        }
        if value.get("all") is True:
            selected = current_ids
        else:
            requested = value.get("notification_ids") or []
            if not isinstance(requested, list):
                raise PipelineError("notification_ids 必须是数组")
            if len(requested) > 500:
                raise PipelineError("单次最多处理 500 条通知")
            selected = {str(item) for item in requested} & current_ids
        self.notifications.mark_read(identity["username"], selected)
        return self.notification_center(identity["username"])

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
        state["product_settings"] = self.product_settings(pipeline_id)
        state["product_version_count"] = len(
            self._product_versions_document(pipeline_id)["versions"]
        )
        return state

    def read_artifact(self, pipeline_id: Any, stage: int) -> dict[str, Any]:
        return _read_object(self.path(pipeline_id, stage))

    def _batch_import_path(self, job_id: Any) -> Path:
        value = str(job_id or "").strip().lower()
        if not _BATCH_JOB_ID.fullmatch(value):
            raise PipelineError("批量任务 ID 格式无效")
        return self._batch_import_root / f"{value}.json"

    def batch_import_status(self, job_id: Any) -> dict[str, Any]:
        path = self._batch_import_path(job_id)
        if not path.exists():
            raise PipelineError("批量导入任务不存在", status=404)
        return _read_object(path)

    def list_batch_imports(self, limit: Any = 20) -> dict[str, Any]:
        try:
            safe_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            safe_limit = 20
        if not self._batch_import_root.exists():
            return {"jobs": []}
        jobs: list[dict[str, Any]] = []
        for path in sorted(
            self._batch_import_root.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                jobs.append(_read_object(path))
            except PipelineError:
                continue
            if len(jobs) >= safe_limit:
                break
        return {"jobs": jobs}

    @staticmethod
    def _batch_import_tokens(value: Any) -> list[str]:
        if isinstance(value, str):
            tokens = re.split(r"[\s,，;；]+", value)
        elif isinstance(value, list):
            tokens = [str(item or "") for item in value]
        else:
            raise PipelineError("items 必须是链接/ID 数组或多行文本")
        return [token.strip() for token in tokens if token.strip()]

    def _batch_quota_snapshot(self, pipeline_ids: list[str]) -> dict[str, int]:
        plan_code = str(os.environ.get("ERP_PLAN") or "business").strip().lower()
        plan = PLAN_CATALOG.get(plan_code, PLAN_CATALOG["business"])
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        used = sum(
            1
            for item in self.list_pipelines()
            if str(item.get("created_at") or item.get("updated_at") or "").startswith(month)
        )
        new_count = sum(
            1 for pipeline_id in pipeline_ids if not self._state_path(pipeline_id).exists()
        )
        limit = int(plan["limits"]["products"])
        if used + new_count > limit:
            raise PipelineError(
                f"本次需新建 {new_count} 个商品任务，但本月仅剩 {max(0, limit - used)} 个额度（{used}/{limit}）",
                status=402,
            )
        return {"used": used, "limit": limit, "new": new_count}

    def start_batch_import(self, value: Mapping[str, Any], actor: Any = "") -> dict[str, Any]:
        tokens = self._batch_import_tokens(value.get("items"))
        if not tokens:
            raise PipelineError("请至少输入一个 1688 商品 ID 或链接")
        if len(tokens) > _MAX_BATCH_IMPORT_ITEMS:
            raise PipelineError(f"单次最多导入 {_MAX_BATCH_IMPORT_ITEMS} 条商品")
        if value.get("confirm_external_requests") is not True:
            raise PipelineError("请确认允许逐条调用 AlphaShop 商品详情 API")

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicate_count = 0
        valid_pipeline_ids: list[str] = []
        for token in tokens:
            try:
                pipeline_id = _safe_pipeline_id(extract_offer_id(token))
            except (PipelineError, ValueError) as exc:
                rows.append(
                    {
                        "input": token[:500],
                        "pipeline_id": None,
                        "status": "FAIL",
                        "error": str(exc)[:500],
                    }
                )
                continue
            if pipeline_id in seen:
                duplicate_count += 1
                continue
            seen.add(pipeline_id)
            valid_pipeline_ids.append(pipeline_id)
            rows.append(
                {
                    "input": token[:500],
                    "pipeline_id": pipeline_id,
                    "status": "QUEUED",
                    "error": None,
                }
            )
        if not valid_pipeline_ids:
            raise PipelineError("没有可识别的 1688 商品 ID 或链接")

        quota = self._batch_quota_snapshot(valid_pipeline_ids)
        with self._lock:
            active = [
                thread for thread in self._batch_import_threads.values() if thread.is_alive()
            ]
            if active:
                raise PipelineError("已有批量采集任务正在执行，请等待完成后再创建", status=409)
            job_id = uuid4().hex
            now = _now()
            job = {
                "schema_version": "1.0",
                "job_id": job_id,
                "status": "QUEUED",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "created_by": str(actor or "").strip() or "unknown",
                "total": len(rows),
                "valid_count": len(valid_pipeline_ids),
                "duplicate_count": duplicate_count,
                "completed_count": sum(1 for row in rows if row["status"] == "FAIL"),
                "passed_count": 0,
                "failed_count": sum(1 for row in rows if row["status"] == "FAIL"),
                "quota": quota,
                "items": rows,
                "message": "已进入后台队列",
            }
            path = self._batch_import_path(job_id)
            _atomic_json(path, job)
            thread = threading.Thread(
                target=self._run_batch_import,
                args=(job_id,),
                daemon=True,
                name=f"batch-import-{job_id[:8]}",
            )
            self._batch_import_threads[job_id] = thread
            thread.start()
        return job

    def _run_batch_import(self, job_id: str) -> None:
        path = self._batch_import_path(job_id)
        with self._lock:
            job = _read_object(path)
            job["status"] = "RUNNING"
            job["started_at"] = _now()
            job["updated_at"] = _now()
            job["message"] = "正在逐条采集"
            _atomic_json(path, job)
        for index in range(len(job.get("items") or [])):
            with self._lock:
                job = _read_object(path)
                row = job["items"][index]
                if row.get("status") != "QUEUED":
                    continue
                row["status"] = "RUNNING"
                row["started_at"] = _now()
                job["updated_at"] = _now()
                _atomic_json(path, job)
            try:
                result = self.run_stage_1(row["pipeline_id"])
                status = str(result.get("status") or "FAIL")
                error = "；".join(str(item) for item in result.get("failures") or []) or None
            except Exception as exc:
                status = "FAIL"
                error = str(exc)[:1000]
            with self._lock:
                job = _read_object(path)
                row = job["items"][index]
                row["status"] = "PASS" if status == "PASS" else "FAIL"
                row["error"] = error
                row["finished_at"] = _now()
                completed = [
                    item
                    for item in job["items"]
                    if item.get("status") in {"PASS", "FAIL", "INTERRUPTED"}
                ]
                job["completed_count"] = len(completed)
                job["passed_count"] = sum(1 for item in completed if item.get("status") == "PASS")
                job["failed_count"] = sum(1 for item in completed if item.get("status") == "FAIL")
                job["updated_at"] = _now()
                _atomic_json(path, job)
        with self._lock:
            job = _read_object(path)
            passed = int(job.get("passed_count") or 0)
            failed = int(job.get("failed_count") or 0)
            job["status"] = "COMPLETED" if failed == 0 else ("FAILED" if passed == 0 else "PARTIAL")
            job["finished_at"] = _now()
            job["updated_at"] = _now()
            job["message"] = f"批量采集完成：成功 {passed}，失败 {failed}"
            _atomic_json(path, job)
            self._batch_import_threads.pop(job_id, None)

    def _batch_process_path(self, job_id: Any) -> Path:
        value = str(job_id or "").strip().lower()
        if not _BATCH_JOB_ID.fullmatch(value):
            raise PipelineError("批量处理任务 ID 格式无效")
        return self._batch_process_root / f"{value}.json"

    def batch_process_status(self, job_id: Any) -> dict[str, Any]:
        path = self._batch_process_path(job_id)
        if not path.exists():
            raise PipelineError("批量处理任务不存在", status=404)
        return _read_object(path)

    def list_batch_processes(self, limit: Any = 20) -> dict[str, Any]:
        try:
            safe_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            safe_limit = 20
        if not self._batch_process_root.exists():
            return {"jobs": []}
        jobs: list[dict[str, Any]] = []
        for path in sorted(
            self._batch_process_root.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                jobs.append(_read_object(path))
            except PipelineError:
                continue
            if len(jobs) >= safe_limit:
                break
        return {"jobs": jobs}

    def start_batch_process(
        self, value: Mapping[str, Any], actor: Any = ""
    ) -> dict[str, Any]:
        raw_ids = value.get("pipeline_ids")
        if not isinstance(raw_ids, list):
            raise PipelineError("pipeline_ids 必须是商品 ID 数组")
        pipeline_ids: list[str] = []
        for raw_id in raw_ids:
            pipeline_id = _safe_pipeline_id(raw_id)
            if pipeline_id not in pipeline_ids:
                pipeline_ids.append(pipeline_id)
        if not pipeline_ids:
            raise PipelineError("请至少选择一个商品")
        if len(pipeline_ids) > _MAX_BATCH_PROCESS_ITEMS:
            raise PipelineError(f"单次最多处理 {_MAX_BATCH_PROCESS_ITEMS} 个商品")
        try:
            target_stage = int(value.get("target_stage"))
        except (TypeError, ValueError) as exc:
            raise PipelineError("目标阶段必须是 2、3 或 4") from exc
        if target_stage not in {2, 3, 4}:
            raise PipelineError("批量处理仅允许运行到第 2、3 或 4 阶段")
        if value.get("confirm_external_requests") is not True:
            raise PipelineError("请确认允许批量调用 AlphaShop、AI 和 Ozon 只读接口")

        store_id = str(value.get("store_id") or "").strip()
        if not store_id:
            raise PipelineError("请选择目标 Ozon 店铺")
        # Validate the configured store without persisting any credentials in the job.
        selected_store = self._selected_store(pipeline_ids[0], store_id)
        confidence_threshold = value.get("confidence_threshold", 0.70)
        try:
            threshold = float(confidence_threshold)
        except (TypeError, ValueError) as exc:
            raise PipelineError("置信度阈值必须是数字") from exc
        if not 0 <= threshold <= 1:
            raise PipelineError("置信度阈值必须在 0–1 之间")
        media_mode = str(value.get("media_mode") or "image_plan").strip().lower()
        text_mode = str(value.get("text_mode") or "ai_generate").strip().lower()
        if media_mode not in {"alphashop_translate", "ai_generate", "image_plan"}:
            raise PipelineError("批量图片模式无效")
        if text_mode not in {"built_in", "ai_generate"}:
            raise PipelineError("批量文本模式无效")
        store_config = value.get("store_config") or {}
        if not isinstance(store_config, Mapping):
            raise PipelineError("批量店铺配置必须是对象")
        dimension_defaults = store_config.get("dimension_defaults") or {}
        if not isinstance(dimension_defaults, Mapping):
            raise PipelineError("批量尺寸与重量配置必须是对象")
        safe_dimensions = {
            key: dimension_defaults.get(key)
            for key in (
                "depth",
                "width",
                "height",
                "weight",
                "dimension_unit",
                "weight_unit",
            )
        }
        safe_store_config = {
            "store_id": str(selected_store["store_id"]),
            "cny_to_rub_rate": store_config.get("cny_to_rub_rate"),
            "dimension_defaults": safe_dimensions,
            "vat": str(store_config.get("vat") or "0"),
            # Category-specific attributes and SKU-specific costs must never be
            # copied across unrelated products by a shared batch request.
            "attribute_overrides": {},
            "sku_price_overrides": {},
        }

        rows: list[dict[str, Any]] = []
        for pipeline_id in pipeline_ids:
            state_path = self._state_path(pipeline_id)
            if not state_path.exists():
                rows.append(
                    {
                        "pipeline_id": pipeline_id,
                        "status": "FAIL",
                        "current_stage": None,
                        "stage_results": [],
                        "error": "商品工作区不存在",
                    }
                )
                continue
            state = _read_object(state_path)
            stage1 = (state.get("stages") or {}).get("1") or {}
            if stage1.get("status") != "PASS" or not self.path(pipeline_id, 1).exists():
                rows.append(
                    {
                        "pipeline_id": pipeline_id,
                        "status": "FAIL",
                        "current_stage": 1,
                        "stage_results": [],
                        "error": "阶段 1 未通过，不能进入批量处理",
                    }
                )
                continue
            rows.append(
                {
                    "pipeline_id": pipeline_id,
                    "status": "QUEUED",
                    "current_stage": 2,
                    "stage_results": [],
                    "error": None,
                }
            )
        if not any(row["status"] == "QUEUED" for row in rows):
            raise PipelineError("所选商品均未通过第一阶段")

        with self._lock:
            active = [
                thread
                for thread in self._batch_process_threads.values()
                if thread.is_alive()
            ]
            if active:
                raise PipelineError("已有批量处理任务正在执行，请等待完成后再创建", status=409)
            job_id = uuid4().hex
            now = _now()
            failed_count = sum(1 for row in rows if row["status"] == "FAIL")
            job = {
                "schema_version": "1.0",
                "job_id": job_id,
                "status": "QUEUED",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "created_by": str(actor or "").strip() or "unknown",
                "target_stage": target_stage,
                "store_id": str(selected_store["store_id"]),
                "confidence_threshold": threshold,
                "media_mode": media_mode,
                "text_mode": text_mode,
                "store_config": safe_store_config,
                "external_requests_confirmed": True,
                "total": len(rows),
                "completed_count": failed_count,
                "passed_count": 0,
                "failed_count": failed_count,
                "items": rows,
                "message": "已进入后台队列；第五阶段不在批处理范围内",
            }
            path = self._batch_process_path(job_id)
            _atomic_json(path, job)
            thread = threading.Thread(
                target=self._run_batch_process,
                args=(job_id,),
                daemon=True,
                name=f"batch-process-{job_id[:8]}",
            )
            self._batch_process_threads[job_id] = thread
            thread.start()
        return job

    def _run_batch_process(self, job_id: str) -> None:
        path = self._batch_process_path(job_id)
        with self._lock:
            job = _read_object(path)
            job["status"] = "RUNNING"
            job["started_at"] = _now()
            job["updated_at"] = _now()
            job["message"] = f"正在逐件运行到阶段 {job['target_stage']}"
            _atomic_json(path, job)
        for index in range(len(job.get("items") or [])):
            with self._lock:
                job = _read_object(path)
                row = job["items"][index]
                if row.get("status") != "QUEUED":
                    continue
                row["status"] = "RUNNING"
                row["started_at"] = _now()
                job["updated_at"] = _now()
                _atomic_json(path, job)
            results: list[dict[str, Any]] = []
            try:
                pipeline_id = str(row["pipeline_id"])
                target_stage = int(job["target_stage"])
                for stage_number in range(2, target_stage + 1):
                    state = self.get_pipeline(pipeline_id)
                    existing_status = str(
                        ((state.get("stages") or {}).get(str(stage_number)) or {}).get("status")
                        or "LOCKED"
                    )
                    if existing_status == "PASS" and self.path(pipeline_id, stage_number).exists():
                        results.append({"stage": stage_number, "status": "SKIPPED_PASS"})
                        continue
                    with self._lock:
                        current = _read_object(path)
                        current["items"][index]["current_stage"] = stage_number
                        current["updated_at"] = _now()
                        _atomic_json(path, current)
                    if stage_number == 2:
                        result = self.run_stage_2(
                            pipeline_id,
                            confidence_threshold=job["confidence_threshold"],
                            store_id=job["store_id"],
                        )
                    elif stage_number == 3:
                        result = self.run_stage_3(
                            pipeline_id,
                            media_mode=job["media_mode"],
                            text_mode=job["text_mode"],
                        )
                    else:
                        result = self.run_stage_4(
                            pipeline_id,
                            store_config=job["store_config"],
                        )
                    stage_status = str(result.get("status") or "FAIL")
                    failures = [str(item) for item in result.get("failures") or []]
                    results.append(
                        {
                            "stage": stage_number,
                            "status": stage_status,
                            "failures": failures[:20],
                        }
                    )
                    if stage_status != "PASS":
                        raise PipelineError(
                            "；".join(failures) or f"阶段 {stage_number} 未通过门禁"
                        )
                status = "PASS"
                error = None
            except Exception as exc:
                status = "FAIL"
                error = str(exc)[:1000]
            with self._lock:
                job = _read_object(path)
                row = job["items"][index]
                row["status"] = status
                row["error"] = error
                row["stage_results"] = results
                row["finished_at"] = _now()
                completed = [
                    item
                    for item in job["items"]
                    if item.get("status") in {"PASS", "FAIL", "INTERRUPTED"}
                ]
                job["completed_count"] = len(completed)
                job["passed_count"] = sum(1 for item in completed if item.get("status") == "PASS")
                job["failed_count"] = sum(1 for item in completed if item.get("status") == "FAIL")
                job["updated_at"] = _now()
                _atomic_json(path, job)
        with self._lock:
            job = _read_object(path)
            passed = int(job.get("passed_count") or 0)
            failed = int(job.get("failed_count") or 0)
            job["status"] = "COMPLETED" if failed == 0 else ("FAILED" if passed == 0 else "PARTIAL")
            job["finished_at"] = _now()
            job["updated_at"] = _now()
            job["message"] = (
                f"批量处理完成：成功 {passed}，失败 {failed}；"
                "未运行第五阶段，未执行真实上架"
            )
            _atomic_json(path, job)
            self._batch_process_threads.pop(job_id, None)

    def run_stage_1(self, offer_id: Any) -> dict[str, Any]:
        try:
            pipeline_id = _safe_pipeline_id(extract_offer_id(offer_id))
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc
        if not self._state_path(pipeline_id).exists():
            self.ensure_product_quota()
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

    def category_options(
        self,
        pipeline_id: Any,
        *,
        query: Any,
        store_id: Any = None,
    ) -> dict[str, Any]:
        """Search selectable category/type pairs from the assigned Ozon store."""

        pipeline_id = _safe_pipeline_id(pipeline_id)
        self._require_previous(pipeline_id, 2)
        search = str(query or "").strip()
        if len(search) < 2:
            raise PipelineError("请输入至少 2 个中文或俄文类目关键词")
        if len(search) > 80:
            raise PipelineError("类目关键词不能超过 80 个字符")
        store = self._selected_store(pipeline_id, store_id)
        client = OzonDescriptionCategoryClient(store["client_id"], store["api_key"])
        try:
            ru_candidates = flatten_category_types(client.category_tree(language="RU"))
            try:
                zh_candidates = flatten_category_types(
                    client.category_tree(language="ZH_HANS")
                )
            except OzonApiError:
                zh_candidates = []
        except OzonApiError as exc:
            raise PipelineError(f"读取 Ozon 官方类目失败：{exc}", status=502) from exc

        merged: dict[tuple[int, int], dict[str, Any]] = {}
        for item in ru_candidates:
            pair = (int(item["ozon_category_id"]), int(item["ozon_type_id"]))
            merged[pair] = {
                "ozon_category_id": pair[0],
                "ozon_type_id": pair[1],
                "category_name_ru": str(item.get("category_name_ru") or ""),
                "category_path_ru": list(item.get("category_path_ru") or []),
                "type_name_ru": str(item.get("type_name_ru") or ""),
            }
        for item in zh_candidates:
            pair = (int(item["ozon_category_id"]), int(item["ozon_type_id"]))
            target = merged.setdefault(
                pair,
                {
                    "ozon_category_id": pair[0],
                    "ozon_type_id": pair[1],
                    "category_name_ru": "",
                    "category_path_ru": [],
                    "type_name_ru": "",
                },
            )
            target["category_name_zh"] = str(item.get("category_name_ru") or "")
            target["category_path_zh"] = list(item.get("category_path_ru") or [])
            target["type_name_zh"] = str(item.get("type_name_ru") or "")

        normalized_query = _category_search_text(search)
        terms = [normalized_query]
        for key, aliases in _CATEGORY_SEARCH_ALIASES.items():
            if key in search:
                terms.extend(_category_search_text(alias) for alias in aliases)
        ranked: list[dict[str, Any]] = []
        for item in merged.values():
            zh_text = _category_search_text(
                " ".join(
                    [
                        *item.get("category_path_zh", []),
                        item.get("category_name_zh", ""),
                        item.get("type_name_zh", ""),
                    ]
                )
            )
            ru_text = _category_search_text(
                " ".join(
                    [
                        *item.get("category_path_ru", []),
                        item.get("category_name_ru", ""),
                        item.get("type_name_ru", ""),
                    ]
                )
            )
            score = 0
            matched_terms: list[str] = []
            for index, term in enumerate(dict.fromkeys(terms)):
                if not term:
                    continue
                if term in zh_text:
                    score += 100 if index == 0 else 35
                    matched_terms.append(term)
                elif term in ru_text:
                    score += 80 if index == 0 else 25
                    matched_terms.append(term)
            if score:
                ranked.append({**item, "score": score, "matched_terms": matched_terms})
        ranked.sort(
            key=lambda item: (
                -int(item["score"]),
                str(item.get("type_name_zh") or item.get("type_name_ru") or ""),
                int(item["ozon_category_id"]),
                int(item["ozon_type_id"]),
            )
        )
        return {
            "query": search,
            "options": ranked[:80],
            "total_matches": len(ranked),
            "source": "Ozon /v1/description-category/tree",
        }

    def run_stage_2(
        self,
        pipeline_id: Any,
        *,
        confidence_threshold: Any = CONFIDENCE_THRESHOLD,
        store_id: Any = None,
        manual_category_id: Any = None,
        manual_type_id: Any = None,
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
        manual_values = (manual_category_id, manual_type_id)
        if any(value not in (None, "") for value in manual_values):
            if not all(value not in (None, "") for value in manual_values):
                raise PipelineError("人工选择类目时必须同时提供 category_id 和 type_id")
            try:
                preferred_pair = (int(manual_category_id), int(manual_type_id))
            except (TypeError, ValueError) as exc:
                raise PipelineError("人工类目和商品类型 ID 必须是正整数") from exc
            if preferred_pair[0] <= 0 or preferred_pair[1] <= 0:
                raise PipelineError("人工类目和商品类型 ID 必须是正整数")
        else:
            preferred_pair = None
        with self._lock:
            raw = self.read_artifact(pipeline_id, 1)
            source_category_id = raw.get("category_id")
            category_exchange = None
            if preferred_pair is None:
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
            if (
                int(mapped.get("ozon_category_id") or 0),
                int(mapped.get("ozon_type_id") or 0),
            ) != preferred_pair:
                raise PipelineError(
                    "所选 category/type 已不在当前 Ozon 店铺的可创建末级类目中，请重新搜索选择",
                    status=409,
                )
            mapped["category_search_input"] = {
                "category_id": raw.get("category_id"),
                "category_path_zh": raw.get("category_path_zh"),
                "title_zh": raw.get("title_zh"),
            }
            if category_exchange is not None:
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
                mapped["category_selection_source"] = "alphashop_auto"
            else:
                mapped["category_selection_source"] = "manual_ozon_tree"
                mapped["manual_category_selection"] = {
                    "ozon_category_id": preferred_pair[0],
                    "ozon_type_id": preferred_pair[1],
                    "verified_against_live_tree": True,
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
            inspection = self._mirror_generated_images(pipeline_id, inspection)
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
                "object_storage": self.object_storage.public_status(),
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
                    "upstream_public_url": item.get("upstream_public_url"),
                    "storage_status": item.get("storage_status"),
                    "storage_object_key": item.get("storage_object_key"),
                    "storage_sha256": item.get("storage_sha256"),
                    "storage_size_bytes": item.get("storage_size_bytes"),
                    "storage_bucket": item.get("storage_bucket"),
                    "storage_endpoint": item.get("storage_endpoint"),
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
            raw = deepcopy(self.read_artifact(pipeline_id, 1))
            price_overrides = store_config.get("sku_price_overrides") or {}
            if not isinstance(price_overrides, Mapping):
                raise PipelineError("SKU 采购价补充值必须是对象")
            sku_rows = [
                item for item in raw.get("sku_list") or [] if isinstance(item, dict)
            ]
            sku_by_id = {
                str(item.get("sku_id")): item
                for item in sku_rows
                if item.get("sku_id") is not None
            }
            unknown_skus = sorted(set(map(str, price_overrides)) - set(sku_by_id))
            if unknown_skus:
                raise PipelineError(
                    f"采购价补充值包含未知 SKU：{', '.join(unknown_skus)}"
                )
            price_override_provenance: list[dict[str, Any]] = []
            for raw_sku_id, raw_price in price_overrides.items():
                sku_id = str(raw_sku_id)
                if raw_price in (None, ""):
                    continue
                try:
                    price = Decimal(str(raw_price).strip())
                except (InvalidOperation, ValueError) as exc:
                    raise PipelineError(f"SKU {sku_id} 的人民币采购价不是数字") from exc
                if not price.is_finite() or price <= 0:
                    raise PipelineError(f"SKU {sku_id} 的人民币采购价必须大于 0")
                sku_by_id[sku_id]["purchase_price_cny"] = str(price)
                price_override_provenance.append(
                    {
                        "sku_id": sku_id,
                        "purchase_price_cny": str(price),
                        "source": "ERP_MANUAL_INPUT",
                    }
                )
            dimensions = store_config.get("dimension_defaults") or {}
            if not isinstance(dimensions, Mapping):
                raise PipelineError("尺寸与重量配置必须是对象")
            try:
                dimension_factor = Decimal("0.1") if str(dimensions.get("dimension_unit") or "mm").lower() == "mm" else Decimal("1")
                weight_factor = Decimal("1000") if str(dimensions.get("weight_unit") or "g").lower() == "kg" else Decimal("1")
                pricing_dimensions = {
                    "length_cm": Decimal(str(dimensions.get("depth"))) * dimension_factor,
                    "width_cm": Decimal(str(dimensions.get("width"))) * dimension_factor,
                    "height_cm": Decimal(str(dimensions.get("height"))) * dimension_factor,
                    "weight_g": Decimal(str(dimensions.get("weight"))) * weight_factor,
                }
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise PipelineError("尺寸与重量必须是有效数字") from exc
            pricing_settings = self.finance.get(str(authorized_store["store_id"]))
            if ozon_currency != "CNY" and store_config.get("cny_to_rub_rate") not in (None, ""):
                pricing_settings = {
                    **pricing_settings,
                    "cny_to_rub_rate": store_config.get("cny_to_rub_rate"),
                }
            pricing_provenance: list[dict[str, Any]] = []
            for sku_id, sku in sku_by_id.items():
                try:
                    pricing = calculate_target_pricing(
                        {
                            **pricing_settings,
                            **pricing_dimensions,
                            "purchase_cost_cny": sku.get("purchase_price_cny"),
                        }
                    )
                except PricingError as exc:
                    raise PipelineError(f"SKU {sku_id} 自动定价失败：{exc}") from exc
                sku["sale_price_cny"] = pricing["suggested_price_cny"]
                pricing_provenance.append(
                    {
                        "sku_id": sku_id,
                        "purchase_cost_cny": str(sku.get("purchase_price_cny")),
                        "sale_price_cny": pricing["suggested_price_cny"],
                        "channel": pricing["channel"],
                        "target_margin_percent": pricing_settings["target_margin_percent"],
                        "source": "WORKBOOK_TARGET_PRICING",
                    }
                )
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
                "cny_to_rub_rate": (
                    "1" if ozon_currency == "CNY" else store_config.get("cny_to_rub_rate")
                ),
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
                "sku_price_override_provenance": price_override_provenance,
                "target_pricing_provenance": pricing_provenance,
                "finance_settings": pricing_settings,
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

    def _fallback_source_images_by_offer(
        self, pipeline_id: str, assembly: AssemblyResult
    ) -> dict[str, list[str]]:
        """Map assembled SKU images back to their original 1688 URLs."""

        final = self.read_artifact(pipeline_id, 3)
        source_by_final: dict[str, str] = {}
        all_sources: list[str] = []
        for row in final.get("image_processing") or []:
            if not isinstance(row, Mapping):
                continue
            final_url = row.get("final_public_url")
            source_url = row.get("source_url")
            if not (
                isinstance(source_url, str)
                and source_url.startswith("https://")
            ):
                continue
            all_sources.append(source_url)
            if isinstance(final_url, str) and final_url:
                source_by_final[final_url] = source_url
        all_sources = list(dict.fromkeys(all_sources))
        result: dict[str, list[str]] = {}
        for item in assembly.payload.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            offer_id = str(item.get("offer_id") or "")
            mapped = [
                source_by_final[url]
                for url in item.get("images") or []
                if isinstance(url, str) and url in source_by_final
            ]
            # Old stage-four artifacts may predate exact per-image provenance.
            # The selected original set is still a stable recovery source.
            if offer_id:
                result[offer_id] = list(dict.fromkeys(mapped or all_sources))[:15]
        return result

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
            fallback_source_images = self._fallback_source_images_by_offer(
                pipeline_id, assembly
            )
            result = run_upload_workflow(
                store=store,
                assembly=assembly,
                upload_log=upload_log,
                execute=execute,
                client=client,
                fallback_source_images_by_offer=fallback_source_images,
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
            if passed:
                # Ozon is already committed at this point. Asset index failures must
                # never turn a successful external submission into a retryable upload.
                try:
                    self._protect_published_assets(pipeline_id)
                except (AssetRegistryError, PipelineError, OSError):
                    pass
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
