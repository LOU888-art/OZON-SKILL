from __future__ import annotations

import unittest

from ozon_skill.category_mapping.mapper import map_listing


class _Provider:
    mode = "test"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.tree_calls = 0
        self.attribute_calls = 0

    def category_tree(self):
        self.tree_calls += 1
        raise AssertionError("verified manual selection must not reload the category tree")

    def category_attributes(self, category_id: int, type_id: int):
        self.attribute_calls += 1
        self.calls.append({"endpoint": "attributes", "category": category_id, "type": type_id})
        return {"result": []}

    def attribute_values(self, category_id: int, type_id: int, attribute_id: int):
        raise AssertionError("empty attributes must not load dictionary values")


class CategoryMappingFastPathTests(unittest.TestCase):
    def test_verified_manual_category_skips_second_tree_scan(self) -> None:
        provider = _Provider()
        result = map_listing(
            {"source_offer_id": "1688-1", "title_zh": "防寒手套"},
            provider,
            preferred_category_type=(123, 456),
            verified_category={
                "ozon_category_id": 123,
                "ozon_type_id": 456,
                "category_path_zh": ["服饰", "手套"],
                "type_name_zh": "保暖手套",
            },
        )

        self.assertEqual(provider.tree_calls, 0)
        self.assertEqual(provider.attribute_calls, 1)
        self.assertEqual(result["ozon_category_id"], 123)
        self.assertEqual(result["ozon_type_id"], 456)
        self.assertTrue(result["alphashop_category_candidate_validated"])

    def test_verified_pair_must_match_submitted_pair(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            map_listing(
                {"source_offer_id": "1688-1"},
                _Provider(),
                preferred_category_type=(123, 999),
                verified_category={"ozon_category_id": 123, "ozon_type_id": 456},
            )


if __name__ == "__main__":
    unittest.main()
