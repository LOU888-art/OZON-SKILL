"""Image reachability and OCR gates; no credential values are logged."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
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

