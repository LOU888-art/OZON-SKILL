"""Image reachability and OCR gates; no credential values are logged."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
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
            "ocr_status": "REVIEWED",
            "contains_chinese": False,
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
    "МОДУЛЬ: главное изображение. Композиция: товар крупно, насыщенная и выразительная "
    "сцена, заголовок и 1–2 подтвержденных преимущества справа. Положение: главный "
    "ракурс целиком. Фон: релевантная реальная среда применения. Свет: контрастный "
    "профессиональный, без потери деталей. Цвет: эстетика российского маркетплейса, "
    "согласованная с цветом товара. Текст: название и короткие преимущества по-русски.",
    "МОДУЛЬ: параметры. Композиция: минималистичная информационная сетка, размерные "
    "линии и карточки только с известными параметрами. Положение: товар вертикально "
    "по центру, полностью виден спереди. Фон: чистый белый. Свет: ровный мягкий без "
    "жестких теней. Цвет: белый и темно-серый. Текст: проверенные размеры и свойства.",
    "МОДУЛЬ: промо. Композиция: крупный товар, слева краткое резюме, снизу округлая "
    "кнопка. Положение: слегка под углом, объемно. Фон: светло-серый, деликатно "
    "размытый интерьер. Свет: объемный коммерческий, яркий и комфортный. Цвет: серый "
    "с одним акцентным цветом. Текст: подтвержденный итог и «Купить сейчас».",
    "МОДУЛЬ: сравнение. Композиция: две колонки; слева типовая старая конструкция, "
    "справа этот товар крупнее, снизу итог. Положение: симметрично, правый товар имеет "
    "визуальный приоритет. Фон: единый светло-серый. Свет: симметричный мягкий, справа "
    "чуть ярче. Цвет: малонасыщенный серый и акцентный цвет. Текст: только честные, "
    "подтвержденные различия без унижения конкурента.",
    "МОДУЛЬ: использование человеком. Композиция: взрослая российская женщина и товар "
    "в естественном масштабе, при уместности небольшой фрагмент результата. Положение: "
    "реалистичное безопасное действие, анатомически точные руки и контакт. Фон: "
    "подходящее фактам место использования. Свет: естественный коммерческий. Цвет: "
    "теплый нейтральный. Текст: короткая русская подсказка о применении.",
    "МОДУЛЬ: преимущества. Композиция: один крупный главный ракурс, две функциональные "
    "метки, простые иконки и один круглый макро-фрагмент. Положение: по центру или "
    "слегка под углом. Фон: чистый нейтральный. Свет: направленный мягкий. Цвет: единая "
    "палитра серии. Текст: ровно 1–2 подтвержденных ключевых преимущества.",
    "МОДУЛЬ: сценарии. Композиция: коллаж из 2–3 реальных сценариев, совместимых именно "
    "с этим товаром; не использовать несвязанные примеры. Положение: товар присутствует "
    "в каждом секторе под разным углом. Фон: реальные места применения. Свет: "
    "согласованный естественный. Цвет: единая рамка или цветовая полоса. Текст: "
    "краткие русские названия сценариев.",
    "МОДУЛЬ: детали. Композиция: локальная часть товара занимает более 50% кадра, "
    "макросъемка материала, соединений и конструкции. Положение: самый информативный "
    "крупный план. Фон: очень чистый светло-серый или бежевый. Свет: профессиональный "
    "микроконтраст. Цвет: нейтральный премиальный. Текст: минимальные русские подписи "
    "только рядом с показанными деталями.",
)


def _listing_image_prompt(
    source: Mapping[str, Any], module_number: int, *, sku_main: bool = False
) -> str:
    facts = {
        "product_name": source.get("title_zh"),
        "attributes": source.get("attributes_zh"),
        "category": source.get("category_path_zh"),
    }
    if sku_main:
        module = _DETAIL_MODULES[0]
        purpose = "Это отдельное главное изображение конкретного SKU."
    else:
        module = _DETAIL_MODULES[module_number - 1]
        purpose = f"Это модуль {module_number} из единой серии из 8 изображений."
    return (
        "Создай вертикальное товарное изображение для российского OZON. "
        "Композиция строго 3:4; файл создается в ближайшем поддерживаемом вертикальном "
        "размере. Сохрани сам товар, его форму, цвет, конструкцию и комплектацию по "
        "референсу без подмены модели. Единый коммерческий стиль серии, согласованные "
        "цвета и визуальный язык без логотипа и выдуманного бренда. Весь видимый текст "
        "только на естественном русском языке; короткий, грамотный и читаемый. Не "
        "придумывай параметры, преимущества и способы применения, которых нет во "
        "входных фактах. Обязательные аспекты: композиция, положение товара, фон или "
        "сцена, профессиональный свет, цветовая гамма и русский текст. "
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
    translator: AlphaShopClient,
    source: Mapping[str, Any],
    *,
    progress: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
    existing_results: Sequence[Mapping[str, Any]] = (),
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
            translated = translate_all_images([item], translator)[0]
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
        for module_number in module_numbers:
            cached_row = cached.get(
                (source_url, action, module_number, str(item.get("sku_id")))
            )
            if cached_row is not None:
                validate_public_image_url(str(cached_row["final_public_url"]))
                results.append(cached_row)
                checkpoint()
                continue
            record: dict[str, Any] = {
                "source_url": source_url,
                "source_role": item.get("source_role"),
                "sku_id": item.get("sku_id"),
                "plan_action": action,
                "module_number": module_number,
                "download_status": "NOT_REQUIRED_REMOTE_API",
                "ocr_status": "REVIEWED",
                "contains_chinese": False,
                "translation_status": "FAILED",
                "final_public_url": None,
                "generation_provider": "AI_RELAY",
                "generation_model": relay.image_model,
                "image_usage": (
                    "sku_main"
                    if action == "sku_main"
                    else "shared_main"
                    if module_number == 1
                    else "shared_gallery"
                ),
                "shared_across_skus": action == "reference_8",
            }
            try:
                validate_public_image_url(source_url)
                generated_url = _generate_with_retry(
                    relay,
                    source_url,
                    str(item.get("source_role") or "") or None,
                    source,
                    _listing_image_prompt(
                        source, module_number, sku_main=action == "sku_main"
                    ),
                )
                validate_public_image_url(generated_url)
                record.update(
                    {
                        "translation_status": "AI_GENERATED",
                        "final_public_url": generated_url,
                    }
                )
                results.append(record)
                checkpoint()
            except (AiRelayError, ValueError) as exc:
                record["error"] = str(exc)[:500]
                results.append(record)
                checkpoint()
                if isinstance(exc, AiRelayError):
                    raise
    return results

