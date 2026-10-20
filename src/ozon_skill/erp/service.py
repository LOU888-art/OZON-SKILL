"""Five-stage pipeline orchestration and auditable gates for the web ERP."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
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
from ozon_skill.content_generation.media import generate_all_images, translate_all_images
from ozon_skill.content_generation.rich_content import (
    BANNED_IMAGE_HOSTS,
    validate_public_image_url,
    validate_rich_content,
)
from ozon_skill.ozon.client import OzonDescriptionCategoryClient
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
        result.append({"url": url, "source_role": "sku_image"})
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
        if media_mode not in {"alphashop_translate", "ai_generate"}:
            raise PipelineError("图片模式必须是 AlphaShop 翻译或 AI 生图")
        if text_mode not in {"built_in", "ai_generate"}:
            raise PipelineError("文本模式必须是内置规则或 AI 生成")
        with self._lock:
            raw = self.read_artifact(pipeline_id, 1)
            mapped = self.read_artifact(pipeline_id, 2)
            try:
                if media_mode == "ai_generate":
                    relay = self.ai_provider.client()
                    inspection = generate_all_images(_image_items(raw), relay, raw)
                else:
                    inspection = translate_all_images(
                        _image_items(raw), AlphaShopClient.from_environment()
                    )
            except AlphaShopInsufficientPointsError as exc:
                failures = [str(exc)]
                self._record(pipeline_id, 3, False, failures)
                raise PipelineError(str(exc), status=402) from exc
            except (AiRelayError, AiProviderConfigurationError) as exc:
                failures = [str(exc)]
                self._record(pipeline_id, 3, False, failures)
                raise PipelineError(str(exc), status=502) from exc
            generated_copy = None
            if text_mode == "ai_generate":
                try:
                    generated_copy = self.ai_provider.client().generate_listing_copy(
                        _source_for_mapper(raw), mapped
                    )
                except (AiRelayError, AiProviderConfigurationError) as exc:
                    failures = [str(exc)]
                    self._record(pipeline_id, 3, False, failures)
                    raise PipelineError(str(exc), status=502) from exc
            final = build_final_listing(
                mapped,
                _source_for_mapper(raw),
                inspection,
                generated_copy=generated_copy,
            )
            final["generation_options"] = {
                "media_mode": media_mode,
                "text_mode": text_mode,
                "ai_relay": self.ai_provider.public_status(),
            }
            final["localized_images"] = [
                {
                    "source_url": item.get("source_url"),
                    "final_public_url": item.get("final_public_url"),
                    "contains_chinese": item.get("contains_chinese"),
                    "translation_status": item.get("translation_status"),
                    "error": item.get("error"),
                }
                for item in inspection
            ]
            failures = gate_stage_3(final)
            _atomic_json(self.path(pipeline_id, 3), final)
            self._record(pipeline_id, 3, not failures, failures)
            return {"stage": 3, "status": "PASS" if not failures else "FAIL", "failures": failures, "artifact": STAGE_FILES[3]}

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
        if normalized_media_mode not in {"alphashop_translate", "ai_generate"}:
            raise PipelineError("图片模式必须是 AlphaShop 翻译或 AI 生图")
        if normalized_text_mode not in {"built_in", "ai_generate"}:
            raise PipelineError("文本模式必须是内置规则或 AI 生成")

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
            })
            self._record(pipeline_id, 4, not failures, failures)
            self._assign_store(pipeline_id, str(authorized_store["store_id"]))
            return {"stage": 4, "status": "PASS" if not failures else "FAIL", "failures": failures, "warnings": assembly.warnings, "artifact": STAGE_FILES[4]}

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
            if not execute and (result.get("request_sent") or result.get("task_id") is not None):
                failures.append("dry-run 日志出现请求或 task_id")
            if execute:
                if result.get("task_id") is None:
                    failures.append("执行模式未取得 task_id")
                if result.get("status") != "VERIFIED":
                    failures.append(f"执行未完成属性回查验证：{result.get('status')}")
                if not isinstance(result.get("attribute_verification"), Mapping):
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
