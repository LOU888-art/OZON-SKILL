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
OCR_CONFIDENCE = 60.0
OCR_SINGLE_CHARACTER_CONFIDENCE = 85.0


def _parse_tesseract_tsv(raw: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for line in raw.splitlines()[1:]:
        columns = line.split("\t", 11)
        if len(columns) != 12:
            continue
        try:
            confidence = float(columns[10])
        except ValueError:
            continue
        text = columns[11].strip()
        chinese = "".join(
            character for character in text if "\u3400" <= character <= "\u9fff"
        )
        if confidence < OCR_CONFIDENCE or not chinese:
            continue
        findings.append(
            {
                "text": text,
                "chinese": chinese,
                "confidence": confidence,
            }
        )
    return findings


def _confirmed_chinese(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Require agreement between two layout modes instead of trusting one OCR guess."""

    first_text = "".join(str(item["chinese"]) for item in first)
    second_text = "".join(str(item["chinese"]) for item in second)
    if not first_text or not second_text:
        return False, ""
    common = set(first_text) & set(second_text)
    if len(first_text) >= 2 and len(second_text) >= 2 and (
        common or (len(first_text) >= 3 and len(second_text) >= 3)
    ):
        return True, "".join(sorted(common)) or first_text[:8]
    first_strong = {
        character
        for item in first
        if float(item["confidence"]) >= OCR_SINGLE_CHARACTER_CONFIDENCE
        for character in str(item["chinese"])
    }
    second_strong = {
        character
        for item in second
        if float(item["confidence"]) >= OCR_SINGLE_CHARACTER_CONFIDENCE
        for character in str(item["chinese"])
    }
    strong_common = first_strong & second_strong
    return bool(strong_common), "".join(sorted(strong_common))


def _run_tesseract(
    path: Path, languages: list[str], psm: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "tesseract",
            str(path),
            "stdout",
            "-l",
            "+".join(languages),
            "--psm",
            str(psm),
            "tsv",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )


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
            # Sparse and uniform-block layouts make different mistakes. Chinese
            # is considered real only when both independently agree, which
            # avoids treating stylised Cyrillic, icons or product texture as a
            # Chinese character.
            results = [
                _run_tesseract(path, language_set, 11),
                _run_tesseract(path, language_set, 6),
            ]
        except (OSError, subprocess.SubprocessError):
            return {"ocr_status": "FAILED", "contains_chinese": None}
    if any(result.returncode != 0 for result in results):
        return {"ocr_status": "FAILED", "contains_chinese": None}
    passes = [_parse_tesseract_tsv(result.stdout) for result in results]
    confirmed, confirmed_text = _confirmed_chinese(passes[0], passes[1])
    suspected = "".join(
        str(item["chinese"]) for findings in passes for item in findings
    )
    return {
        "ocr_status": "COMPLETED",
        "contains_chinese": confirmed,
        "ocr_detection_method": "dual_layout_consensus_v2",
        "ocr_text_preview": confirmed_text[:100],
        "chinese_character_count": len(confirmed_text),
        "suspected_chinese_preview": suspected[:100],
        "ocr_pass_character_counts": [
            sum(len(str(item["chinese"])) for item in findings) for findings in passes
        ],
    }
