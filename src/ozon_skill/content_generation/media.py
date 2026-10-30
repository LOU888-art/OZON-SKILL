"""Image reachability and OCR gates; no credential values are logged."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ozon_skill.ai_relay.client import AiRelayClient, AiRelayError
from ozon_skill.alphashop.client import (
    AlphaShopApiError,
    AlphaShopClient,
    AlphaShopInsufficientPointsError,
)

from .rich_content import validate_public_image_url


def _has_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in text)


def _tesseract_languages() -> set[str]:
    if shutil.which("tesseract") is None:
        return set()
    result = subprocess.run(
        ["tesseract", "--list-langs"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    lines = (result.stdout + result.stderr).splitlines()
    return {line.strip() for line in lines if line.strip() and "available languages" not in line}


class FreeImageHostUploader:
    """Upload a reviewed image without exposing the API key in logs or output."""

    endpoint = "https://freeimage.host/api/1/upload"

    def __init__(self, api_key: str, *, timeout_seconds: float = 60.0) -> None:
        if not api_key:
            raise ValueError("FREEIMAGE_HOST_API_KEY must not be empty")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds

    def upload(self, path: Path) -> str:
        data = path.read_bytes()
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError("translated image must be between 1 byte and 20 MiB")
        body = urlencode(
            {
                "key": self._api_key,
                "action": "upload",
                "source": base64.b64encode(data).decode("ascii"),
                "format": "json",
            }
        ).encode("ascii")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "ozon-skill-media-uploader/0.1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"freeimage.host returned HTTP {exc.code}") from exc
        except (URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("freeimage.host upload failed") from exc
        image = payload.get("image") if isinstance(payload, Mapping) else None
        url = image.get("url") if isinstance(image, Mapping) else None
        if not isinstance(url, str):
            raise RuntimeError("freeimage.host response did not contain an image URL")
        validate_public_image_url(url)
        return url


def inspect_images(
    image_items: Sequence[Mapping[str, Any]], *, timeout_seconds: float = 15.0
) -> list[dict[str, Any]]:
    """Download and OCR images; failures remain explicit and never imply safety."""

    languages = _tesseract_languages()
    results: list[dict[str, Any]] = []
    for item in image_items:
        url = str(item.get("url") or "")
        record: dict[str, Any] = {
            "source_url": url or None,
            "download_status": "NOT_RUN",
            "ocr_status": "NOT_RUN",
            "contains_chinese": None,
            "translation_status": "NOT_REQUIRED_UNKNOWN",
            "final_public_url": None,
        }
        try:
            validate_public_image_url(url)
            request = Request(url, headers={"User-Agent": "ozon-skill-media-check/0.1"})
            with urlopen(request, timeout=timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("image exceeds the 20 MiB inspection limit")
            if not content_type.lower().startswith("image/"):
                raise ValueError(f"unexpected content type: {content_type or 'missing'}")
            record["download_status"] = "DOWNLOADED"
            if "chi_sim" not in languages:
                record["ocr_status"] = "BLOCKED_MISSING_CHINESE_MODEL"
                record["error"] = "tesseract language chi_sim is not installed"
                results.append(record)
                continue
            suffix = ".png" if "png" in content_type.lower() else ".jpg"
            with tempfile.TemporaryDirectory(prefix="ozon-ocr-") as directory:
                path = Path(directory) / f"image{suffix}"
                path.write_bytes(data)
                ocr = subprocess.run(
                    ["tesseract", str(path), "stdout", "-l", "chi_sim+eng"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            if ocr.returncode != 0:
                record["ocr_status"] = "FAILED"
                record["error"] = "tesseract could not process the image"
            else:
                contains_chinese = _has_cjk(ocr.stdout)
                record["ocr_status"] = "COMPLETED"
                record["contains_chinese"] = contains_chinese
                record["translation_status"] = (
                    "NEEDS_TRANSLATION" if contains_chinese else "NOT_REQUIRED"
                )
                if not contains_chinese:
                    record["final_public_url"] = url
            results.append(record)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            record["download_status"] = "FAILED"
            record["error"] = str(exc)[:240]
            results.append(record)
    return results


def apply_verified_media_manifest(
    inspection: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    uploader: FreeImageHostUploader | None = None,
) -> list[dict[str, Any]]:
    """Apply reviewed OCR/translation/upload results without fabricating URLs."""

    reviewed = manifest.get("images") if isinstance(manifest, Mapping) else None
    if not isinstance(reviewed, list):
        raise ValueError("media manifest must contain an images array")
    by_source = {
        item.get("source_url"): item for item in reviewed if isinstance(item, Mapping)
    }
    result: list[dict[str, Any]] = []
    for original in inspection:
        record = dict(original)
        update = by_source.get(original.get("source_url"))
        if update is None:
            result.append(record)
            continue
        contains_chinese = update.get("contains_chinese")
        final_url = update.get("final_public_url")
        translated_file = update.get("translated_file")
        if isinstance(translated_file, str):
            if uploader is None:
                raise ValueError(
                    "FREEIMAGE_HOST_API_KEY is required to upload translated_file"
                )
            final_url = uploader.upload(Path(translated_file))
        if not isinstance(contains_chinese, bool) or not isinstance(final_url, str):
            raise ValueError(
                "reviewed media requires boolean OCR result and final_public_url or translated_file"
            )
        validate_public_image_url(final_url)
        record.update(
            {
                "ocr_status": "REVIEWED",
                "contains_chinese": contains_chinese,
                "translation_status": (
                    "TRANSLATED_AND_UPLOADED" if contains_chinese else "NOT_REQUIRED"
                ),
                "final_public_url": final_url,
                "reviewed": True,
            }
        )
        record.pop("error", None)
        result.append(record)
    return result


def translate_all_images(
    image_items: Sequence[Mapping[str, Any]], client: AlphaShopClient
) -> list[dict[str, Any]]:
    """Translate every supplied image to Russian through AlphaShop PRO."""

    results: list[dict[str, Any]] = []
    for item in image_items:
        source_url = str(item.get("url") or "")
        record: dict[str, Any] = {
            "source_url": source_url or None,
            "source_role": item.get("source_role"),
            "download_status": "NOT_REQUIRED_REMOTE_API",
            "ocr_status": "REVIEWED",
            "contains_chinese": None,
            "translation_status": "FAILED",
            "final_public_url": None,
        }
        try:
            validate_public_image_url(source_url)
            translated_url, exchange = client.translate_image(source_url)
            validate_public_image_url(translated_url)
            canonical = json.dumps(
                exchange.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            record.update(
                {
                    "translation_status": "TRANSLATED_AND_UPLOADED",
                    "final_public_url": translated_url,
                    "alphashop_response_sha256": hashlib.sha256(canonical).hexdigest(),
                }
            )
        except AlphaShopInsufficientPointsError:
            raise
        except (AlphaShopApiError, ValueError) as exc:
            record["error"] = str(exc)[:500]
        results.append(record)
    return results


def generate_all_images(
    image_items: Sequence[Mapping[str, Any]],
    client: AiRelayClient,
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Generate one Russian marketplace image for every supplied source image."""

    results: list[dict[str, Any]] = []
    for item in image_items:
        source_url = str(item.get("url") or "")
        record: dict[str, Any] = {
            "source_url": source_url or None,
            "source_role": item.get("source_role"),
            "download_status": "NOT_REQUIRED_REMOTE_API",
            "ocr_status": "NOT_RUN",
            "contains_chinese": None,
            "translation_status": "FAILED",
            "final_public_url": None,
            "generation_provider": "AI_RELAY",
            "generation_model": client.image_model,
        }
        try:
            validate_public_image_url(source_url)
            generated_url = client.generate_product_image(
                source_url, str(item.get("source_role") or "") or None, source
            )
            validate_public_image_url(generated_url)
            record.update(
                {
                    "translation_status": "AI_GENERATED",
                    "final_public_url": generated_url,
                }
            )
        except AiRelayError:
            # Provider-wide failures should stop before spending more calls.
            raise
        except ValueError as exc:
            record["error"] = str(exc)[:500]
        results.append(record)
    return results


_DETAIL_MODULES = (
    "КАДР 1 — ГЛАВНОЕ ИЗОБРАЖЕНИЕ. Создай кликабельную главную фотографию по точным "
    "поисковым намерениям покупателей Ozon и понятной российской эстетике. Товар занимает "
    "60–75% кадра, его внешний вид не меняется; фон заменить на правдивую сцену использования. "
    "В свободной зоне разместить естественное русское название и 1–2 подтвержденных преимущества. "
    "Обновить шрифт, цвет и композицию текста, не закрывая товар. Без цены, скидки и рейтинга.",
    "КАДР 2 — ПАРАМЕТРЫ. Чистый белый фон, товар строго в центре, полностью виден и не "
    "деформирован. Показать спецификацию и размерные линии только для размеров, которые "
    "явно присутствуют в фактах; при отсутствии размеров не придумывать числа, а показать "
    "подтвержденные цвет, количество, форму или комплектацию. Короткие русские подписи.",
    "КАДР 3 — ИТОГ И ПРИЗЫВ К ДЕЙСТВИЮ. Коммерческая промо-композиция с кратким честным "
    "резюме товара и естественным русским призывом «Выберите свой вариант» или «Закажите "
    "сейчас». Не изображать несуществующую акцию, цену, скидку, таймер, подарок или гарантию.",
    "КАДР 4 — СРАВНЕНИЕ ДИЗАЙНА. Слева или сверху — наш товар как главный выбор, рядом — "
    "нейтральный безымянный силуэт другого распространенного дизайна. Сравнивать только "
    "видимые и доказуемые особенности формы, конструкции или комплектации. Наш товар должен "
    "выделяться композицией, но нельзя выдумывать испытания, превосходство, бренд конкурента "
    "или писать «лучший». Русские подписи короткие и проверяемые.",
    "КАДР 5 — ИСПОЛЬЗОВАНИЕ ЧЕЛОВЕКОМ. Реалистичный взрослый человек российской внешности "
    "держит или использует товар строго по назначению. Масштаб рук, человека и товара "
    "анатомически правдивый, взаимодействие естественное. Добавить одну короткую русскую "
    "фразу о подтвержденном способе использования.",
    "КАДР 6 — КЛЮЧЕВЫЕ ПРЕИМУЩЕСТВА. Показать 1–2 наиболее важных подтвержденных свойства "
    "с лаконичными русскими формулировками и простыми аккуратными пиктограммами. Не повторять "
    "общие слова «высокое качество» и не заявлять свойства, которых нет в исходных данных.",
    "КАДР 7 — СЦЕНАРИИ ИСПОЛЬЗОВАНИЯ. Единая естественная коммерческая композиция показывает "
    "два или три правдивых жизненных сценария, подходящих товару. Сцены выглядят живыми, не "
    "жесткими и не постановочными; товар везде сохраняет форму, цвет и реальный масштаб. "
    "Не превращать изображение в перегруженную сетку и не добавлять неподтвержденный эффект.",
    "КАДР 8 — ДЕТАЛИ И КАЧЕСТВО. Макросъемка только реально видимых материала, фактуры, "
    "шва, поверхности, соединения или элемента конструкции. Подчеркнуть качество светом и "
    "композицией, не выдумывая обратную сторону, внутреннее устройство, материал или свойства. "
    "До трех коротких русских подписей.",
)


def _image_safe_facts(value: Any) -> Any:
    """Keep factual structure while removing Chinese copy the image model may echo."""

    if isinstance(value, Mapping):
        return {
            str(key): cleaned
            for key, item in value.items()
            if (cleaned := _image_safe_facts(item)) not in (None, "", [], {})
        }
    if isinstance(value, (list, tuple)):
        return [
            cleaned
            for item in value
            if (cleaned := _image_safe_facts(item)) not in (None, "", [], {})
        ]
    if isinstance(value, str):
        cleaned = re.sub(r"[\u3400-\u9fff]+", " ", value)
        return re.sub(r"\s+", " ", cleaned).strip() or None
    return value


def _listing_image_prompt(
    source: Mapping[str, Any], module_number: int, *, sku_main: bool = False
) -> str:
    sku_id = None
    sku_specification = None
    if sku_main:
        # The caller supplies a SKU reference image; expose only the matching
        # structured facts so the model does not blend neighboring variants.
        sku_id = source.get("_active_sku_id")
        sku_specification = source.get("_active_sku_specification")
    facts = _image_safe_facts({
        "product_name": source.get("title_zh"),
        "attributes": source.get("attributes_zh"),
        "category": source.get("category_path_zh"),
        "sku_id": sku_id,
        "sku_specification": sku_specification,
    })
    module = _DETAIL_MODULES[module_number - 1]
    if sku_main:
        purpose = (
            f"Это кадр {module_number} отдельной серии из 8 изображений конкретного SKU; "
            "не смешивай цвет, аромат, количество или упаковку с другими вариантами."
        )
    else:
        purpose = f"Это модуль {module_number} из единой серии из 8 изображений."
    return (
        "Ты — старший визуальный эксперт российского OZON, коммерческий фотограф и "
        "русскоязычный редактор карточек. Создай ОДИН отдельный вертикальный файл из "
        "серии 1+7, строго 3:4, целевой размер 1200x1600 или ближайший больший. Это не "
        "коллаж, не сетка и не длинное изображение. Исходное фото — единственный эталон "
        "внешности товара. Строго сохрани форму, конструкцию, цвет, пропорции, фактуру, "
        "отверстия, разъемы, детали и видимые комплектующие. Не дорисовывай обратную "
        "сторону или внутренность, которых не видно. Не меняй количество и не добавляй "
        "подарки, функции, размеры, материал, характеристики, логотип, бренд, награды "
        "или водяной знак: водяной знак при необходимости будет наложен системой отдельно. "
        "Реквизит — только фон, не часть продажи. Все восемь кадров должны иметь единую "
        "визуальную систему и совпадать с первым кадром по палитре, типографике и характеру "
        "света. Современный стиль российской электронной коммерции: спокойные белые, светло-серые, "
        "кремовые и теплые оттенки по категории; профессиональный коммерческий свет, высокая "
        "резкость и ясный товар для мобильного экрана. Весь текст только на кратком, "
        "естественном и безошибочном русском языке, без китайского, пиньиня и лишнего "
        "английского; текст не закрывает товар. Если факт не подтвержден, не показывай "
        "его и не заменяй выдуманным. "
        f"{purpose} {module} ФАКТЫ: "
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    )


def _generate_with_retry(
    client: AiRelayClient,
    source_url: str,
    source_role: str | None,
    source: Mapping[str, Any],
    prompt: str,
) -> str:
    last_error: AiRelayError | None = None
    for attempt, delay in enumerate((0, 3, 8), start=1):
        if delay:
            time.sleep(delay)
        try:
            return client.generate_product_image(
                source_url,
                source_role,
                source,
                prompt_override=prompt,
            )
        except AiRelayError as exc:
            last_error = exc
            message = str(exc)
            if not any(f"HTTP {status}" in message for status in (500, 502, 503)):
                raise
            if attempt == 3:
                raise
    assert last_error is not None
    raise last_error


def process_image_plan(
    image_items: Sequence[Mapping[str, Any]],
    relay: AiRelayClient,
    translator: AlphaShopClient | None,
    source: Mapping[str, Any],
    *,
    progress: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
    existing_results: Sequence[Mapping[str, Any]] = (),
    max_workers: int = 1,
) -> list[dict[str, Any]]:
    """Run per-image translate, shared-eight, and SKU-main actions."""

    results: list[dict[str, Any]] = []
    cached = {
        (
            str(row.get("source_url")),
            str(row.get("plan_action")),
            row.get("module_number"),
            str(row.get("sku_id")),
        ): dict(row)
        for row in existing_results
        if isinstance(row, Mapping) and row.get("final_public_url")
    }

    def checkpoint() -> None:
        if progress is not None:
            progress(results)

    for item in image_items:
        action = str(item.get("action") or "translate")
        source_url = str(item.get("url") or "")
        if action == "translate":
            cached_row = cached.get((source_url, action, None, str(item.get("sku_id"))))
            if cached_row is not None:
                validate_public_image_url(str(cached_row["final_public_url"]))
                results.append(cached_row)
                checkpoint()
                continue
            translated: dict[str, Any] | None = None
            translation_error: str | None = None
            if translator is not None:
                try:
                    translated = translate_all_images([item], translator)[0]
                except AlphaShopApiError as exc:
                    # Image translation is optional.  Missing credentials or
                    # insufficient AlphaShop points must never block listing.
                    translation_error = str(exc)[:500]
            if translated is None:
                if hasattr(relay, "translate_product_image"):
                    generated_url = relay.translate_product_image(
                        source_url,
                        str(item.get("source_role") or "") or None,
                        source,
                    )
                else:
                    generated_url = _generate_with_retry(
                        relay,
                        source_url,
                        str(item.get("source_role") or "") or None,
                        source,
                        _listing_image_prompt(source, 2, sku_main=False),
                    )
                validate_public_image_url(generated_url)
                translated = {
                    "source_url": source_url,
                    "source_role": item.get("source_role"),
                    "sku_id": item.get("sku_id"),
                    "download_status": "NOT_REQUIRED_REMOTE_API",
                    "ocr_status": "NOT_RUN",
                    "contains_chinese": None,
                    "translation_status": "AI_GENERATED",
                    "final_public_url": generated_url,
                    "generation_provider": "AI_RELAY",
                    "generation_model": getattr(
                        relay, "image_translation_model", relay.image_model
                    ),
                    "translation_fallback_reason": translation_error or "ALPHASHOP_UNAVAILABLE",
                }
            translated.update(
                {
                    "plan_action": action,
                    "image_usage": "rich_content",
                    "shared_across_skus": True,
                }
            )
            results.append(translated)
            checkpoint()
            continue
        module_numbers = range(1, 9) if action == "reference_8" else (1,)
        pending_modules: list[int] = []
        for module_number in module_numbers:
            cached_row = cached.get(
                (source_url, action, module_number, str(item.get("sku_id")))
            )
            if cached_row is not None:
                validate_public_image_url(str(cached_row["final_public_url"]))
                results.append(cached_row)
                checkpoint()
                continue
            pending_modules.append(module_number)

        def generate_module(module_number: int) -> tuple[dict[str, Any], Exception | None]:
            record: dict[str, Any] = {
                "source_url": source_url,
                "source_role": item.get("source_role"),
                "sku_id": item.get("sku_id"),
                "plan_action": action,
                "module_number": module_number,
                "download_status": "NOT_REQUIRED_REMOTE_API",
                "ocr_status": "NOT_RUN",
                "contains_chinese": None,
                "translation_status": "FAILED",
                "final_public_url": None,
                "generation_provider": "AI_RELAY",
                "generation_model": relay.image_model,
                "image_usage": (
                    "sku_main"
                    if item.get("sku_id") and module_number == 1
                    else "sku_gallery"
                    if item.get("sku_id")
                    else "shared_main"
                    if module_number == 1
                    else "shared_gallery"
                ),
                "shared_across_skus": action == "reference_8" and not item.get("sku_id"),
            }
            try:
                validate_public_image_url(source_url)
                prompt_source = dict(source)
                if item.get("sku_id"):
                    prompt_source["_active_sku_id"] = item.get("sku_id")
                    prompt_source["_active_sku_specification"] = item.get(
                        "specification_zh"
                    )
                generated_url = _generate_with_retry(
                    relay,
                    source_url,
                    str(item.get("source_role") or "") or None,
                    prompt_source,
                    _listing_image_prompt(
                        prompt_source,
                        module_number,
                        sku_main=bool(item.get("sku_id")),
                    ),
                )
                validate_public_image_url(generated_url)
                record.update(
                    {
                        "translation_status": "AI_GENERATED",
                        "final_public_url": generated_url,
                    }
                )
            except (AiRelayError, ValueError) as exc:
                record["error"] = str(exc)[:500]
                return record, exc
            return record, None

        if not pending_modules:
            continue
        worker_count = max(1, min(int(max_workers), 6, len(pending_modules)))
        first_ai_error: AiRelayError | None = None
        if worker_count == 1:
            generated = [generate_module(module) for module in pending_modules]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="ai-image",
            ) as executor:
                generated = list(executor.map(generate_module, pending_modules))
        for record, error in generated:
            results.append(record)
            checkpoint()
            if isinstance(error, AiRelayError) and first_ai_error is None:
                first_ai_error = error
        if first_ai_error is not None:
            raise first_ai_error
    return results

