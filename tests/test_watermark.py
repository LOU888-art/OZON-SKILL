from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

from ozon_skill.content_generation.watermark import (
    apply_watermark_bytes,
    normalize_watermark_settings,
)


def _png(color: tuple[int, int, int, int], size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class WatermarkTests(unittest.TestCase):
    def test_disabled_watermark_preserves_original_bytes(self) -> None:
        source = _png((255, 255, 255, 255), (300, 400))
        self.assertEqual(apply_watermark_bytes(source, {"enabled": False}), source)

    def test_text_watermark_supports_cyrillic_and_position(self) -> None:
        source = _png((255, 255, 255, 255), (600, 800))
        result = apply_watermark_bytes(
            source,
            {
                "enabled": True,
                "type": "text",
                "text": "МОЙ МАГАЗИН",
                "size_percent": 25,
                "opacity_percent": 70,
                "position": "bottom_right",
                "color": "#112233",
            },
        )
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (600, 800))
            self.assertNotEqual(image.getpixel((560, 760)), (255, 255, 255, 255))

    def test_image_watermark_is_scaled_and_applied(self) -> None:
        source = _png((255, 255, 255, 255), (600, 800))
        logo = _png((255, 0, 0, 255), (200, 100))
        result = apply_watermark_bytes(
            source,
            {
                "enabled": True,
                "type": "image",
                "image_url": "https://cdn.example.com/logo.png",
                "size_percent": 20,
                "opacity_percent": 50,
                "position": "center",
                "color": "#FFFFFF",
            },
            logo_bytes=logo,
        )
        with Image.open(BytesIO(result)) as image:
            self.assertNotEqual(image.getpixel((300, 400)), (255, 255, 255, 255))

    def test_invalid_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_watermark_settings(
                {
                    "enabled": True,
                    "type": "image",
                    "image_url": "http://example.com/logo.png",
                }
            )


if __name__ == "__main__":
    unittest.main()
