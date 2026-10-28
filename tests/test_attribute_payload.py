from __future__ import annotations

import unittest

from ozon_skill.upload.assembler import _api_attributes


class AttributePayloadTests(unittest.TestCase):
    def test_decimal_attribute_is_normalized_before_ozon_submission(self) -> None:
        payload, errors, _ = _api_attributes(
            [
                {
                    "attribute_id": 6392,
                    "attribute_name_ru": "Вес товара, г",
                    "attribute_type": "Decimal",
                    "is_dictionary": False,
                    "value": "120,50 г",
                }
            ],
            {"sku_id": "sku-a"},
        )

        self.assertEqual(errors, [])
        self.assertEqual(payload[0]["values"], [{"value": "120.5"}])

    def test_invalid_decimal_is_blocked_during_payload_assembly(self) -> None:
        payload, errors, _ = _api_attributes(
            [
                {
                    "attribute_id": 6392,
                    "attribute_type": "Decimal",
                    "is_dictionary": False,
                    "value": "большой",
                }
            ],
            {"sku_id": "sku-b"},
        )

        self.assertEqual(payload, [])
        self.assertIn("数值属性 6392 的 SKU sku-b", errors[0])

    def test_hashtags_use_one_value_and_each_token_respects_symbol_limit(self) -> None:
        payload, errors, _ = _api_attributes(
            [
                {
                    "attribute_id": 23171,
                    "attribute_name_ru": "Хештег",
                    "is_collection": True,
                    "max_count": 30,
                    "is_dictionary": False,
                    "value": [
                        "аромат для дома",
                        "очень длинный декоративный ароматический аксессуар для комнаты",
                    ],
                }
            ]
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(payload[0]["values"]), 1)
        tags = payload[0]["values"][0]["value"].split()
        self.assertEqual(len(tags), 2)
        self.assertTrue(
            all(len(tag) <= 30 for tag in tags)
        )
        self.assertTrue(all(" " not in tag for tag in tags))

    def test_dictionary_uses_direct_validated_id_when_projection_is_missing(self) -> None:
        attributes = [
            {
                "attribute_id": 7405,
                "is_required": True,
                "is_dictionary": True,
                "value": "Для дома",
                "dictionary_value_id": 123456,
                "dictionary_matches": [],
            }
        ]

        payload, errors, required_ids = _api_attributes(attributes)

        self.assertEqual(errors, [])
        self.assertEqual(required_ids, [7405])
        self.assertEqual(
            payload,
            [
                {
                    "complex_id": 0,
                    "id": 7405,
                    "values": [
                        {"dictionary_value_id": 123456, "value": "Для дома"}
                    ],
                }
            ],
        )

    def test_invalid_optional_dictionary_text_is_safely_omitted(self) -> None:
        attributes = [
            {
                "attribute_id": 7405,
                "is_required": False,
                "is_dictionary": True,
                "value": "无法映射的旧文本",
                "dictionary_matches": [],
            }
        ]

        payload, errors, required_ids = _api_attributes(attributes)

        self.assertEqual(payload, [])
        self.assertEqual(errors, [])
        self.assertEqual(required_ids, [])

    def test_invalid_required_dictionary_text_still_blocks_payload(self) -> None:
        attributes = [
            {
                "attribute_id": 7405,
                "is_required": True,
                "is_dictionary": True,
                "value": "无法映射的旧文本",
                "dictionary_matches": [],
            }
        ]

        payload, errors, required_ids = _api_attributes(attributes)

        self.assertEqual(payload, [])
        self.assertEqual(required_ids, [7405])
        self.assertIn(
            "字典属性 7405 有文本值但缺少 Ozon dictionary_value_id，请选择官方候选值",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
