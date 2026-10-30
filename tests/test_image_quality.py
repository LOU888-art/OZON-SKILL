from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ozon_skill.content_generation.image_quality import normalize_three_by_four
from ozon_skill.content_generation.media import _image_safe_facts
from ozon_skill.erp.service import PipelineService, gate_stage_3


class ImageQualityTests(unittest.TestCase):
    def test_two_by_three_provider_image_becomes_exact_three_by_four(self) -> None:
        source = Image.new("RGB", (1024, 1536), (230, 220, 200))
        payload = io.BytesIO()
        source.save(payload, format="JPEG")

        result, metadata = normalize_three_by_four(payload.getvalue())

        with Image.open(io.BytesIO(result)) as normalized:
            self.assertEqual(normalized.size, (1200, 1600))
            self.assertEqual(normalized.format, "PNG")
        self.assertEqual(metadata["source_width"], 1024)
        self.assertEqual(metadata["source_height"], 1536)
        self.assertEqual(metadata["width"], 1200)
        self.assertEqual(metadata["height"], 1600)

    def test_image_prompt_facts_do_not_echo_chinese_copy(self) -> None:
        cleaned = _image_safe_facts(
            {"title": "化妆品收纳盒 Organizer", "size": "26.5 cm", "count": 1}
        )
        self.assertEqual(cleaned, {"title": "Organizer", "size": "26.5 cm", "count": 1})

    def test_ai_image_without_quality_proof_is_blocked(self) -> None:
        listing = {
            "title_ru": "О" * 60,
            "title_validation": {"status": "VALID"},
            "title_segments": {"brand": "без бренда"},
            "description_ru": "Описание",
            "tags_ru": ["товар"],
            "localized_images": [
                {
                    "final_public_url": "https://example.com/image.png",
                    "generation_provider": "AI_RELAY",
                    "contains_chinese": False,
                }
            ],
            "ai_gallery_validation": {"all_skus_have_8": True},
        }
        failures = gate_stage_3(listing)
        self.assertTrue(any("1200×1600" in item for item in failures))

    def test_pipeline_uploads_only_normalized_ocr_verified_image(self) -> None:
        source = Image.new("RGB", (1024, 1536), (230, 220, 200))
        payload = io.BytesIO()
        source.save(payload, format="JPEG")

        class _Client:
            prefix = "northstar"
            bucket = "images"
            endpoint = "https://oss-cn-hongkong.aliyuncs.com"

            def put(self, key: str, data: bytes, content_type: str) -> str:
                self.saved = (key, data, content_type)
                return f"https://cdn.example.com/{key}"

        class _Relay:
            def translate_product_image(self, *_args, **_kwargs):
                raise AssertionError("clean image must not consume another AI call")

        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            client = _Client()
            with (
                patch.object(service.object_storage, "client", return_value=client),
                patch(
                    "ozon_skill.erp.service.download_public_image",
                    return_value=(payload.getvalue(), "image/jpeg"),
                ),
                patch(
                    "ozon_skill.erp.service.detect_chinese_text",
                    return_value={"ocr_status": "COMPLETED", "contains_chinese": False},
                ),
            ):
                rows = service._enforce_generated_image_quality(
                    "item",
                    [
                        {
                            "final_public_url": "https://images.example.com/generated.jpg",
                            "plan_action": "reference_8",
                            "generation_provider": "AI_RELAY",
                        }
                    ],
                    _Relay(),
                    {},
                )

        self.assertEqual(rows[0]["quality_status"], "VERIFIED")
        self.assertEqual((rows[0]["width"], rows[0]["height"]), (1200, 1600))
        self.assertEqual(rows[0]["aspect_ratio"], "3:4")
        self.assertIs(rows[0]["contains_chinese"], False)
        with Image.open(io.BytesIO(client.saved[1])) as normalized:
            self.assertEqual(normalized.size, (1200, 1600))


if __name__ == "__main__":
    unittest.main()
