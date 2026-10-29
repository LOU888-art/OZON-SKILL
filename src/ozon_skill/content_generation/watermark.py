"""Deterministic text/logo watermarking for generated product images."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageFont, ImageOps


POSITIONS = {
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
}


def normalize_watermark_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("enabled") is not True:
        return {"enabled": False}
    kind = str(value.get("type") or "text").strip().lower()
    if kind not in {"text", "image"}:
        raise ValueError("水印类型必须是文字或图片")
    try:
        size_percent = int(value.get("size_percent", 18))
        opacity_percent = int(value.get("opacity_percent", 35))
    except (TypeError, ValueError) as exc:
        raise ValueError("水印大小和透明度必须是整数") from exc
    if not 5 <= size_percent <= 40:
        raise ValueError("水印大小必须在 5%–40% 之间")
    if not 5 <= opacity_percent <= 100:
        raise ValueError("水印透明度必须在 5%–100% 之间")
    position = str(value.get("position") or "bottom_right").strip().lower()
    if position not in POSITIONS:
        raise ValueError("水印位置无效")
    text = str(value.get("text") or "").strip()
    image_url = str(value.get("image_url") or "").strip()
    if kind == "text" and not text:
        raise ValueError("文字水印内容不能为空")
    if len(text) > 80:
        raise ValueError("文字水印最多 80 个字符")
    if kind == "image" and not image_url.startswith("https://"):
        raise ValueError("图片水印必须填写公开 HTTPS 图片地址")
    color = str(value.get("color") or "#FFFFFF").strip().upper()
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("文字水印颜色必须是 #RRGGBB 格式")
    try:
        int(color[1:], 16)
    except ValueError as exc:
        raise ValueError("文字水印颜色必须是 #RRGGBB 格式") from exc
    return {
        "enabled": True,
        "type": kind,
        "text": text if kind == "text" else None,
        "image_url": image_url if kind == "image" else None,
        "size_percent": size_percent,
        "opacity_percent": opacity_percent,
        "position": position,
        "color": color,
    }


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fontss/truetype/noto/NotoSans-Bold.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default(size=size)


def _position(
    canvas: tuple[int, int], mark: tuple[int, int], name: str
) -> tuple[int, int]:
    width, height = canvas
    mark_width, mark_height = mark
    margin = max(10, round(min(width, height) * 0.035))
    vertical, horizontal = name.split("_", 1) if "_" in name else (name, "center")
    x = {
        "left": margin,
        "center": (width - mark_width) // 2,
        "right": width - mark_width - margin,
    }[horizontal]
    y = {
        "top": margin,
        "center": (height - mark_height) // 2,
        "bottom": height - mark_height - margin,
    }[vertical]
    return max(0, x), max(0, y)


def apply_watermark_bytes(
    image_bytes: bytes,
    settings: Mapping[str, Any],
    *,
    logo_bytes: bytes | None = None,
) -> bytes:
    """Return PNG bytes with one deterministic watermark applied."""

    normalized = normalize_watermark_settings(settings)
    if not normalized["enabled"]:
        return image_bytes
    with Image.open(BytesIO(image_bytes)) as opened:
        base = ImageOps.exif_transpose(opened).convert("RGBA")
    width, height = base.size
    if width < 64 or height < 64 or width * height > 40_000_000:
        raise ValueError("待加水印图片尺寸不受支持")
    opacity = round(255 * normalized["opacity_percent"] / 100)
    if normalized["type"] == "image":
        if not logo_bytes:
            raise ValueError("图片水印内容为空")
        with Image.open(BytesIO(logo_bytes)) as opened_logo:
            mark = ImageOps.exif_transpose(opened_logo).convert("RGBA")
        target_width = max(20, round(width * normalized["size_percent"] / 100))
        target_height = max(1, round(mark.height * target_width / max(mark.width, 1)))
        mark.thumbnail((target_width, min(target_height, height // 2)), Image.Resampling.LANCZOS)
        alpha = mark.getchannel("A").point(lambda value: value * opacity // 255)
        mark.putalpha(alpha)
    else:
        text = str(normalized["text"])
        target_width = max(40, round(width * normalized["size_percent"] / 100))
        font_size = max(12, min(height // 8, round(target_width / max(1.8, len(text) * 0.62))))
        font = _font(font_size)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        box = probe.textbbox((0, 0), text, font=font, stroke_width=max(1, font_size // 22))
        padding = max(5, font_size // 5)
        mark = Image.new(
            "RGBA", (box[2] - box[0] + padding * 2, box[3] - box[1] + padding * 2), (0, 0, 0, 0)
        )
        color = tuple(bytes.fromhex(normalized["color"][1:])) + (opacity,)
        brightness = sum(color[:3])
        stroke = (0, 0, 0, min(220, opacity)) if brightness > 380 else (255, 255, 255, min(220, opacity))
        ImageDraw.Draw(mark).text(
            (padding - box[0], padding - box[1]),
            text,
            font=font,
            fill=color,
            stroke_width=max(1, font_size // 22),
            stroke_fill=stroke,
        )
    base.alpha_composite(mark, _position(base.size, mark.size, normalized["position"]))
    output = BytesIO()
    base.save(output, format="PNG", optimize=True)
    return output.getvalue()
