from __future__ import annotations

import unittest

from ozon_skill.category_mapping.mapper import _map_attributes


class _Provider:
    def attribute_values(self, category_id: int, type_id: int, attribute_id: int):
        self.request = (category_id, type_id, attribute_id)
        return {"result": [{"id": 64382, "value": "Китай"}]}


class AutomaticAttributeTests(unittest.TestCase):
    def test_country_of_origin_defaults_to_china_and_uses_live_dictionary_id(self) -> None:
        provider = _Provider()
        mapped, confirmations, required_mapped, required_total = _map_attributes(
            provider,
            {"attributes_zh": [], "sku_list": []},
            10,
            20,
            {
                "result": [
                    {
                        "id": 31,
                        "name": "Страна производства",
                        "is_required": True,
                        "is_dictionary": True,
                    }
                ]
            },
        )

        self.assertEqual(confirmations, [])
        self.assertEqual(required_mapped, 1)
        self.assertEqual(required_total, 1)
        self.assertEqual(mapped[0]["value"], "Китай")
        self.assertEqual(mapped[0]["dictionary_value_id"], 64382)
        self.assertEqual(mapped[0]["mapping_status"], "MAPPED_DICTIONARY")
        self.assertEqual(provider.request, (10, 20, 31))


if __name__ == "__main__":
    unittest.main()
