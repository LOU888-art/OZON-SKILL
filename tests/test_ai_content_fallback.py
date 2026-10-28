from __future__ import annotations

import unittest
from decimal import Decimal

from ozon_skill.content_generation.generator import build_final_listing
from ozon_skill.content_generation.media import process_image_plan
from ozon_skill.upload.assembler import assemble_import_payload
from ozon_skill.upload.config import DimensionDefaults, StoreConfig


class _FakeRelay:
    image_model = "test-image-model"

    def generate_product_image(self, *_args, **_kwargs) -> str:
        return "https://cdn.example.com/generated-product.jpg"


class _EightImageRelay:
    image_model = "test-image-model"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate_product_image(self, *_args, **kwargs) -> str:
        self.calls += 1
        self.prompts.append(str(kwargs.get("prompt_override") or ""))
        return f"https://cdn.example.com/generated-{self.calls}.jpg"


class AiContentFallbackTests(unittest.TestCase):
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

    def test_translate_action_uses_ai_when_alphashop_is_unavailable(self) -> None:
        result = process_image_plan(
            [
                {
                    "url": "https://supplier.example.com/source.jpg",
                    "source_role": "main_image",
                    "action": "translate",
                }
            ],
            _FakeRelay(),
            None,
            {"title_zh": "家居香薰"},
        )

        self.assertEqual(result[0]["translation_status"], "AI_GENERATED")
        self.assertEqual(result[0]["image_usage"], "rich_content")
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
