from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ozon_skill.ai_relay.client import _non_chinese_image_facts
from ozon_skill.content_generation.image_quality import (
    _confirmed_chinese,
    _parse_tesseract_tsv,
    _run_tesseract,
    normalize_three_by_four,
)
from ozon_skill.content_generation.media import _image_safe_facts, _listing_image_prompt
from ozon_skill.erp.service import (
    PipelineService,
    _image_review_fingerprint,
    gate_stage_3,
)


class ImageQualityTests(unittest.TestCase):
    def test_tesseract_retries_a_transient_nonzero_exit(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "temporary failure")
        completed = subprocess.CompletedProcess([], 0, "header", "")
        with patch(
            "ozon_skill.content_generation.image_quality.subprocess.run",
            side_effect=[failed, completed],
        ) as runner:
            result = _run_tesseract(Path("/tmp/example.png"), ["chi_sim"], 11)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.kwargs["env"]["OMP_THREAD_LIMIT"], "1")

    def test_ocr_requires_two_layouts_to_agree_on_chinese(self) -> None:
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
            "width\theight\tconf\ttext"
        )
        false_positive = _parse_tesseract_tsv(
            header + "\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t72\t图"
        )
        self.assertEqual(_confirmed_chinese(false_positive, []), (False, ""))

        first = _parse_tesseract_tsv(
            header + "\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t91\t中文"
        )
        second = _parse_tesseract_tsv(
            header + "\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t88\t中文"
        )
        confirmed, evidence = _confirmed_chinese(first, second)
        self.assertTrue(confirmed)
        self.assertTrue(evidence)

    def test_image_repair_prompt_facts_cannot_reinject_chinese(self) -> None:
        cleaned = _non_chinese_image_facts(
            {
                "product_title": "化妆品收纳盒 Organizer",
                "verified_attributes": {"颜色": "绿色 green", "尺寸": "26 cm"},
            }
        )
        rendered = str(cleaned)
        self.assertNotRegex(rendered, r"[\u3400-\u9fff]")
        self.assertIn("Organizer", rendered)
        self.assertIn("green", rendered)

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
            {
                "title": "化妆品收纳盒 Organizer",
                "属性": {"颜色": "绿色 green"},
                "size": "26.5 cm",
                "count": 1,
            }
        )
        self.assertNotRegex(str(cleaned), r"[\u3400-\u9fff]")
        self.assertIn("green", str(cleaned))

    def test_every_product_uses_the_same_universal_visual_identity_lock(self) -> None:
        prompts = [
            _listing_image_prompt(
                {
                    "title_zh": title,
                    "attributes_zh": {"规格": specification},
                },
                5,
            )
            for title, specification in (
                ("加热鼠标垫", "90×40cm"),
                ("化妆品防尘收纳盒", "绿色"),
                ("宠物慢食碗", "蓝色"),
            )
        ]
        for prompt in prompts:
            self.assertIn("визуальный паспорт товара", prompt)
            self.assertIn("Этот паспорт неизменяем во всех восьми кадрах", prompt)
            self.assertIn("не имеет права переопределять внешний вид товара", prompt)
            self.assertIn("При конфликте всегда побеждает исходное фото", prompt)
            self.assertIn("без контакта на нейтральном студийном фоне", prompt)
            self.assertNotRegex(prompt, r"[\u3400-\u9fff]")
        self.assertNotIn("коврик для мыши/клавиатуры", " ".join(prompts))

    def test_ai_gallery_cannot_pass_before_stable_human_review(self) -> None:
        images = [
            {
                "final_public_url": f"https://example.com/image-{index}.png",
                "generation_provider": "AI_RELAY",
                "quality_status": "VERIFIED",
                "contains_chinese": False,
                "width": 1200,
                "height": 1600,
                "aspect_ratio": "3:4",
                "module_number": index,
                "image_usage": "shared_main" if index == 1 else "shared_gallery",
            }
            for index in range(1, 9)
        ]
        listing = {
            "title_ru": (
                "Электрический настольный коврик для мыши и клавиатуры "
                "с регулятором температуры"
            ),
            "title_validation": {"status": "VALID"},
            "title_segments": {"brand": "без бренда"},
            "description_ru": "Настольный коврик для рабочего места.",
            "tags_ru": ["коврик для мыши"],
            "localized_images": images,
            "ai_gallery_validation": {"all_skus_have_8": True},
            "image_review": {"status": "PENDING"},
        }
        pending_failures = gate_stage_3(listing)
        self.assertTrue(any("等待人工" in item for item in pending_failures))

        listing["image_review"] = {
            "status": "APPROVED",
            "approved_fingerprint": _image_review_fingerprint(listing),
        }
        self.assertEqual(gate_stage_3(listing), [])

        listing["localized_images"][0]["final_public_url"] = (
            "https://example.com/changed.png"
        )
        self.assertTrue(any("发生变化" in item for item in gate_stage_3(listing)))

    def test_human_approval_writes_stable_gallery_into_stage_three(self) -> None:
        images = [
            {
                "final_public_url": f"https://example.com/image-{index}.png",
                "generation_provider": "AI_RELAY",
                "quality_status": "VERIFIED",
                "contains_chinese": False,
                "width": 1200,
                "height": 1600,
                "aspect_ratio": "3:4",
                "module_number": index,
                "image_usage": "shared_main" if index == 1 else "shared_gallery",
            }
            for index in range(1, 9)
        ]
        artifact = {
            "title_ru": (
                "Электрический настольный коврик для мыши и клавиатуры "
                "с регулятором температуры"
            ),
            "title_validation": {"status": "VALID"},
            "title_segments": {"brand": "без бренда"},
            "description_ru": "Настольный коврик для рабочего места.",
            "tags_ru": ["коврик для мыши"],
            "localized_images": images,
            "ai_gallery_validation": {"all_skus_have_8": True},
            "image_review": {"status": "PENDING"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "item"
            directory.mkdir()
            (directory / "pipeline.json").write_text(
                json.dumps(
                    {
                        "pipeline_id": "item",
                        "stages": {
                            "1": {"status": "PASS"},
                            "2": {"status": "PASS"},
                            "3": {"status": "FAIL"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (directory / "03_final.json").write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            service = PipelineService(Path(temporary))
            result = service.review_generated_images(
                "item",
                decision="approve",
                confirm_subject_unchanged=True,
                actor="reviewer",
            )
            saved = service.read_artifact("item", 3)
            state = service.get_pipeline("item")

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(saved["image_review"]["status"], "APPROVED")
        self.assertEqual(saved["image_review"]["approved_by"], "reviewer")
        self.assertEqual(state["stages"]["3"]["status"], "PASS")

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

    def test_pipeline_rechecks_and_repairs_confirmed_chinese_twice(self) -> None:
        source = Image.new("RGB", (900, 1200), (230, 220, 200))
        payload = io.BytesIO()
        source.save(payload, format="JPEG")

        class _Client:
            prefix = "northstar"
            bucket = "images"
            endpoint = "https://oss-cn-hongkong.aliyuncs.com"

            def put(self, key: str, data: bytes, content_type: str) -> str:
                return f"https://cdn.example.com/{key}"

        class _Relay:
            def __init__(self) -> None:
                self.calls = 0

            def translate_product_image(self, *_args, **_kwargs) -> str:
                self.calls += 1
                return f"https://images.example.com/repaired-{self.calls}.jpg"

        dirty = {
            "ocr_status": "COMPLETED",
            "contains_chinese": True,
            "ocr_text_preview": "中文",
        }
        clean = {
            "ocr_status": "COMPLETED",
            "contains_chinese": False,
            "ocr_detection_method": "dual_layout_consensus_v2",
        }
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            relay = _Relay()
            with (
                patch.object(service.object_storage, "client", return_value=_Client()),
                patch(
                    "ozon_skill.erp.service.download_public_image",
                    return_value=(payload.getvalue(), "image/jpeg"),
                ),
                patch(
                    "ozon_skill.erp.service.detect_chinese_text",
                    side_effect=[dirty, dirty, clean],
                ),
            ):
                rows = service._enforce_generated_image_quality(
                    "item",
                    [
                        {
                            "final_public_url": "https://images.example.com/generated.jpg",
                            "plan_action": "reference_8",
                        }
                    ],
                    relay,
                    {"title_zh": "化妆品收纳盒"},
                )

        self.assertEqual(relay.calls, 2)
        self.assertEqual(rows[0]["quality_status"], "VERIFIED")
        self.assertEqual(rows[0]["chinese_cleanup_attempts"], 2)
        self.assertEqual(len(rows[0]["ocr_history"]), 3)


if __name__ == "__main__":
    unittest.main()
