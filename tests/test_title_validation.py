from __future__ import annotations

import unittest

from ozon_skill.content_generation.generator import _title_validation


class RussianTitleValidationTests(unittest.TestCase):
    def test_repeated_function_words_do_not_fail_natural_russian_title(self) -> None:
        title = (
            "Ароматическая гипсовая подвеска для дома и шкафа — декор для "
            "спальни и автомобиля, с мягким ароматом для ежедневного уюта и "
            "подарка близким, без бренда"
        )
        result = _title_validation(
            title,
            {"core_product_name": "Ароматическая гипсовая подвеска"},
        )

        self.assertEqual(result["status"], "VALID")
        self.assertNotIn("для", result["repeated_roots_3_or_more"])

    def test_repeated_meaningful_root_blocks_keyword_stuffing(self) -> None:
        title = (
            "Ароматическая подвеска ароматическая для дома — ароматическая "
            "композиция для шкафа, спальни, автомобиля и подарка близким, "
            "практичный декоративный аксессуар без бренда"
        )
        result = _title_validation(
            title,
            {"core_product_name": "Ароматическая подвеска"},
        )

        self.assertEqual(result["status"], "NEEDS_CONFIRMATION")
        self.assertTrue(result["repeated_roots_3_or_more"])
        self.assertTrue(result["issues"])


if __name__ == "__main__":
    unittest.main()
