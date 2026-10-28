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


class AiContentFallbackTests(unittest.TestCase):
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
            "Ароматический диффузор для дома с натуральным ароматом, "
            "декоративный аксессуар спальни, гостиной и офиса, стильный "
            "подарок для создания уютной спокойной атмосферы"
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
                        "final_public_url": "https://cdn.example.com/main.jpg",
                        "image_usage": "shared_main",
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

        self.assertEqual(result.validation_errors, [])
        self.assertEqual(
            result.payload["items"][0]["primary_image"],
            "https://cdn.example.com/main.jpg",
        )


if __name__ == "__main__":
    unittest.main()
