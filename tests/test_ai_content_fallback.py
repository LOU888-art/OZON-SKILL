from __future__ import annotations

import unittest
import threading
import time
from decimal import Decimal

from ozon_skill.content_generation.generator import build_final_listing
from ozon_skill.content_generation.media import process_image_plan
from ozon_skill.upload.assembler import assemble_import_payload
from ozon_skill.upload.config import DimensionDefaults, StoreConfig


class _FakeRelay:
    image_model = "test-image-model"
    image_translation_model = "test-translation-model"

    def __init__(self) -> None:
        self.translation_calls = 0

    def generate_product_image(self, *_args, **_kwargs) -> str:
        return "https://cdn.example.com/generated-product.jpg"

    def translate_product_image(self, *_args, **_kwargs) -> str:
        self.translation_calls += 1
        return "https://cdn.example.com/translated-product.jpg"


class _EightImageRelay:
    image_model = "test-image-model"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate_product_image(self, *_args, **kwargs) -> str:
        self.calls += 1
        self.prompts.append(str(kwargs.get("prompt_override") or ""))
        return f"https://cdn.example.com/generated-{self.calls}.jpg"


class _ConcurrentRelay:
    image_model = "test-image-model"

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def generate_product_image(self, *_args, **_kwargs) -> str:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return f"https://cdn.example.com/generated-{threading.get_ident()}.jpg"
        finally:
            with self.lock:
                self.active -= 1


class AiContentFallbackTests(unittest.TestCase):
    def test_reference_images_run_concurrently_but_keep_module_order(self) -> None:
        relay = _ConcurrentRelay()
        result = process_image_plan(
            [
                {
                    "url": "https://supplier.example.com/source.jpg",
                    "source_role": "images",
                    "action": "reference_8",
                }
            ],
            relay,
            None,
            {"title_zh": "家居香薰"},
            max_workers=4,
        )

        self.assertGreaterEqual(relay.max_active, 2)
        self.assertEqual([item["module_number"] for item in result], list(range(1, 9)))

    def test_reference_plan_generates_eight_independent_ai_images(self) -> None:
        relay = _EightImageRelay()
        result = process_image_plan(
            [
                {
                    "url": "https://supplier.example.com/source.jpg",
                    "source_role": "images",
                    "action": "reference_8",
                }
            ],
            relay,
            None,
            {"title_zh": "家居香薰"},
        )

        self.assertEqual(relay.calls, 8)
        self.assertEqual(len(result), 8)
        self.assertEqual(len({item["final_public_url"] for item in result}), 8)
        self.assertEqual(result[0]["image_usage"], "shared_main")
        self.assertTrue(
            all(item["generation_provider"] == "AI_RELAY" for item in result)
        )
        self.assertIn("КАДР 1", relay.prompts[0])
        self.assertIn("КАДР 8", relay.prompts[-1])
        self.assertIn("ПАРАМЕТРЫ", relay.prompts[1])
        self.assertIn("ПРИЗЫВ К ДЕЙСТВИЮ", relay.prompts[2])
        self.assertIn("СРАВНЕНИЕ ДИЗАЙНА", relay.prompts[3])
        self.assertIn("ИСПОЛЬЗОВАНИЕ ЧЕЛОВЕКОМ", relay.prompts[4])
        self.assertIn("КЛЮЧЕВЫЕ ПРЕИМУЩЕСТВА", relay.prompts[5])
        self.assertIn("СЦЕНАРИИ ИСПОЛЬЗОВАНИЯ", relay.prompts[6])
        self.assertIn("ДЕТАЛИ И КАЧЕСТВО", relay.prompts[7])
        self.assertTrue(all("строго 3:4" in prompt for prompt in relay.prompts))

    def test_translate_action_uses_ai_when_alphashop_is_unavailable(self) -> None:
        relay = _FakeRelay()
        result = process_image_plan(
            [
                {
                    "url": "https://supplier.example.com/source.jpg",
                    "source_role": "main_image",
                    "action": "translate",
                }
            ],
            relay,
            None,
            {"title_zh": "家居香薰"},
        )

        self.assertEqual(result[0]["translation_status"], "AI_GENERATED")
        self.assertEqual(result[0]["image_usage"], "rich_content")
        self.assertEqual(result[0]["generation_model"], "test-translation-model")
        self.assertEqual(relay.translation_calls, 1)
        self.assertEqual(
            result[0]["translation_fallback_reason"], "ALPHASHOP_UNAVAILABLE"
        )

    def test_missing_rich_images_uses_description_without_blocking_copy(self) -> None:
        title = (
            "Ароматический диффузор для дома, декоративный аксессуар "
            "для спальни, гостиной и офиса, вариант подарка близким"
        )
        final = build_final_listing(
            {
                "source_offer_id": "1688-1",
                "ozon_category_id": 2,
                "ozon_type_id": 3,
                "ozon_attributes": [],
                "needs_confirmation": [],
            },
            {},
            [],
            generated_copy={
                "title_ru": title,
                "title_segments": {"core_product_name": "Ароматический диффузор"},
                "description_ru": "Подробное описание товара для покупателя.",
                "tags_ru": ["аромат для дома"],
            },
        )

        self.assertIsNone(final["rich_content_json"])
        self.assertEqual(final["rich_content_strategy"], "DESCRIPTION_ONLY")
        self.assertEqual(final["listing_readiness"], "READY_FOR_PAYLOAD_ASSEMBLY")

    def test_payload_assembly_does_not_require_rich_content(self) -> None:
        result = assemble_import_payload(
            {
                "source_offer_id": "1688-1",
                "title_ru": "Товар",
                "ozon_type_id": 3,
                "ozon_category_id": 2,
                "listing_readiness": "READY_FOR_PAYLOAD_ASSEMBLY",
                "rich_content_json": None,
                "ozon_attributes": [],
                "image_processing": [
                    {
                        "final_public_url": f"https://cdn.example.com/{index}.jpg",
                        "image_usage": "shared_main" if index == 1 else "shared_gallery",
                        "generation_provider": "AI_RELAY",
                        "translation_status": "AI_GENERATED",
                    }
                    for index in range(1, 9)
                ],
            },
            {
                "sku_list": [
                    {
                        "sku_id": "sku-1",
                        "purchase_price_cny": "10",
                        "sale_price_cny": "20",
                    }
                ]
            },
            StoreConfig(
                store_id="store",
                credential_prefix="STORE",
                cny_to_rub_rate=Decimal("12"),
                dimension_defaults=DimensionDefaults(100, 100, 100, 100),
            ),
        )

        self.assertEqual(result.validation_errors, [])
        self.assertEqual(
            result.payload["items"][0]["primary_image"],
            "https://cdn.example.com/1.jpg",
        )
        self.assertEqual(len(result.payload["items"][0]["images"]), 8)
        self.assertEqual(result.payload["items"][0]["price"], "480.00")
        self.assertEqual(result.payload["items"][0]["old_price"], "960.00")

    def test_payload_uses_one_sku_main_and_seven_shared_ai_images(self) -> None:
        shared_gallery = [
            {
                "final_public_url": f"https://cdn.example.com/shared-{index}.jpg",
                "image_usage": "shared_gallery",
                "generation_provider": "AI_RELAY",
                "translation_status": "AI_GENERATED",
            }
            for index in range(2, 9)
        ]
        sku_mains = [
            {
                "final_public_url": f"https://cdn.example.com/{sku_id}-main.jpg",
                "image_usage": "sku_main",
                "sku_id": sku_id,
                "generation_provider": "AI_RELAY",
                "translation_status": "AI_GENERATED",
            }
            for sku_id in ("sku-1", "sku-2")
        ]
        result = assemble_import_payload(
            {
                "source_offer_id": "1688-1",
                "title_ru": "Ароматическая подвеска для шкафа и комнаты",
                "sku_titles_ru": {
                    "sku-1": "Ароматическая подвеска с ароматом розы для шкафа",
                    "sku-2": "Ароматическая подвеска с ароматом лаванды для шкафа",
                },
                "ozon_type_id": 3,
                "ozon_category_id": 2,
                "listing_readiness": "READY_FOR_PAYLOAD_ASSEMBLY",
                "rich_content_json": None,
                "ozon_attributes": [],
                "image_processing": [
                    {
                        "final_public_url": "https://cdn.example.com/shared-main.jpg",
                        "image_usage": "shared_main",
                        "generation_provider": "AI_RELAY",
                        "translation_status": "AI_GENERATED",
                    },
                    *shared_gallery,
                    *sku_mains,
                    {
                        "final_public_url": "https://1688.example.com/original.jpg",
                        "image_usage": "shared_gallery",
                        "generation_provider": "SOURCE_1688",
                        "translation_status": "ORIGINAL",
                    },
                ],
            },
            {
                "sku_list": [
                    {
                        "sku_id": sku_id,
                        "purchase_price_cny": "10",
                        "sale_price_cny": "20",
                    }
                    for sku_id in ("sku-1", "sku-2")
                ]
            },
            StoreConfig(
                store_id="store",
                credential_prefix="STORE",
                cny_to_rub_rate=Decimal("12"),
                dimension_defaults=DimensionDefaults(100, 100, 100, 100),
            ),
        )

        self.assertEqual(result.validation_errors, [])
        self.assertEqual(len(result.payload["items"]), 2)
        for item, sku_id in zip(result.payload["items"], ("sku-1", "sku-2")):
            self.assertEqual(len(item["images"]), 8)
            self.assertEqual(
                item["primary_image"],
                f"https://cdn.example.com/{sku_id}-main.jpg",
            )
            self.assertNotIn(
                "https://cdn.example.com/shared-main.jpg", item["images"]
            )
            self.assertNotIn(
                "https://1688.example.com/original.jpg", item["images"]
            )

    def test_payload_rejects_original_or_translation_only_images(self) -> None:
        result = assemble_import_payload(
            {
                "source_offer_id": "1688-1",
                "title_ru": "Ароматическая подвеска для шкафа и комнаты, декоративный ароматизатор для домашнего пространства",
                "ozon_type_id": 3,
                "ozon_category_id": 2,
                "listing_readiness": "READY_FOR_PAYLOAD_ASSEMBLY",
                "rich_content_json": None,
                "ozon_attributes": [],
                "image_processing": [
                    {
                        "source_url": "https://1688.example.com/original.jpg",
                        "final_public_url": "https://cdn.example.com/translated.jpg",
                        "image_usage": "shared_main",
                        "translation_status": "TRANSLATED_AND_UPLOADED",
                    }
                ],
            },
            {
                "sku_list": [
                    {
                        "sku_id": "sku-1",
                        "purchase_price_cny": "10",
                        "sale_price_cny": "20",
                    }
                ]
            },
            StoreConfig(
                store_id="store",
                credential_prefix="STORE",
                cny_to_rub_rate=Decimal("12"),
                dimension_defaults=DimensionDefaults(100, 100, 100, 100),
            ),
        )

        self.assertEqual(result.payload["items"][0]["images"], [])
        self.assertTrue(
            any("8 张独立 AI 生成图" in error for error in result.validation_errors)
        )


if __name__ == "__main__":
    unittest.main()
