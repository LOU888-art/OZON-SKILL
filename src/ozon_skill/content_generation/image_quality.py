"""Deterministic marketplace image geometry and Chinese OCR checks."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps


TARGET_WIDTH = 1200
TARGET_HEIGHT = 1600


def normalize_three_by_four(image_bytes: bytes) -> tuple[bytes, dict[str, int]]:
    """Preserve the whole image on an exact 1200x1600 canvas without stretching."""

    with Image.open(BytesIO(image_bytes)) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = source.size
    if width < 64 or height < 64 or width * height > 40_000_000:
        raise ValueError("AI图片尺寸不受支持")

    target = (TARGET_WIDTH, TARGET_HEIGHT)
    # A blurred full-bleed copy avoids hard white bars when a provider returns
    # its native 2:3 canvas.  The untouched image is then fitted on top, so no
    # product, label or edge is cropped and the product itself is not distorted.
    background = ImageOps.fit(source, target, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=32))
    foreground = ImageOps.contain(source, target, method=Image.Resampling.LANCZOS)
    left = (TARGET_WIDTH - foreground.width) // 2
    top = (TARGET_HEIGHT - foreground.height) // 2
    background.paste(foreground, (left, top))

    output = BytesIO()
    background.save(output, format="PNG", optimize=True)
    return output.getvalue(), {
        "source_width": width,
        "source_height": height,
        "width": TARGET_WIDTH,
        "height": TARGET_HEIGHT,
    }


def detect_chinese_text(image_bytes: bytes) -> dict[str, Any]:
    """Return a strict OCR result; absence of the Chinese model is explicit."""

    if shutil.which("tesseract") is None:
        return {
            "ocr_status": "BLOCKED_MISSING_TESSERACT",
            "contains_chinese": None,
        }
    try:
        languages = subprocess.run(
            ["tesseract", "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ocr_status": "FAILED", "contains_chinese": None}
    available = {
        line.strip()
        for line in (languages.stdout + languages.stderr).splitlines()
        if line.strip() and "available languages" not in line
    }
    if "chi_sim" not in available:
        return {
            "ocr_status": "BLOCKED_MISSING_CHINESE_MODEL",
            "contains_chinese": None,
        }
    language_set = ["chi_sim"]
    if "rus" in available:
        language_set.append("rus")
    if "eng" in available:
        language_set.append("eng")
    with tempfile.TemporaryDirectory(prefix="ozon-generated-ocr-") as directory:
        path = Path(directory) / "image.png"
        path.write_bytes(image_bytes)
        try:
            result = subprocess.run(
                [
                    "tesseract",
                    str(path),
                    "stdout",
                    "-l",
                    "+".join(language_set),
                    "--psm",
                    "11",
                    "tsv",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            return {"ocr_status": "FAILED", "contains_chinese": None}
    if result.returncode != 0:
        return {"ocr_status": "FAILED", "contains_chinese": None}
    recognized: list[str] = []
    chinese: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split("\t", 11)
        if len(columns) != 12:
            continue
        try:
            confidence = float(columns[10])
        except ValueError:
            continue
        text = columns[11].strip()
        if confidence < 35 or not text:
            continue
        recognized.append(text)
        chinese.extend(
            character for character in text if "\u3400" <= character <= "\u9fff"
        )
    return {
        "ocr_status": "COMPLETED",
        "contains_chinese": bool(chinese),
        "ocr_text_preview": " ".join(recognized)[:500],
        "chinese_character_count": len(chinese),
    }
