from __future__ import annotations

import unittest

from ozon_skill.upload.assembler import _api_attributes


class AttributePayloadTests(unittest.TestCase):
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
