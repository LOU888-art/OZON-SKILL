from __future__ import annotations

import unittest

from ozon_skill.erp.pricing import (
    PricingError,
    calculate_pricing,
    calculate_target_pricing,
)
from ozon_skill.erp.pricing_categories import search_commission_categories


class PricingWorkbookParityTests(unittest.TestCase):
    def test_category_commission_switches_by_ruble_price_band(self) -> None:
        category = search_commission_categories("家用香薰", 1)["results"][0]
        common = {
            "commission_category_id": category["id"],
            "purchase_cost_cny": 10,
            "weight_g": 300,
            "cny_to_rub_rate": 12.6,
        }

        low = calculate_pricing({**common, "sale_price_cny": 100})
        middle = calculate_pricing({**common, "sale_price_cny": 200})
        high = calculate_pricing({**common, "sale_price_cny": 500})

        self.assertEqual(low["commission"]["applied_percent"], "12")
        self.assertEqual(middle["commission"]["applied_percent"], "14")
        self.assertEqual(high["commission"]["applied_percent"], "18")
        self.assertEqual(middle["fees"]["platform_commission"], "28.00")

    def test_suggested_price_reselects_category_commission_tier(self) -> None:
        category = search_commission_categories("家用香薰", 1)["results"][0]
        result = calculate_pricing(
            {
                "commission_category_id": category["id"],
                "sale_price_cny": 100,
                "purchase_cost_cny": 100,
                "weight_g": 300,
                "cny_to_rub_rate": 12.6,
            }
        )

        self.assertEqual(result["commission"]["applied_percent"], "12")
        self.assertTrue(any("佣金将从 12" in item for item in result["warnings"]))
        self.assertGreater(float(result["suggested_price_cny"]) * 12.6, 1500)

    def test_category_search_supports_chinese_and_russian(self) -> None:
        chinese = search_commission_categories("儿童智能手表", 10)
        russian = search_commission_categories("Смарт-часы детские", 10)

        self.assertEqual(chinese["total_categories"], 10788)
        self.assertEqual(chinese["results"][0]["id"], russian["results"][0]["id"])

    def test_unknown_category_commission_is_rejected(self) -> None:
        with self.assertRaisesRegex(PricingError, "未找到选中的 Ozon 类目佣金"):
            calculate_pricing(
                {
                    "commission_category_id": "missing",
                    "sale_price_cny": 100,
                    "purchase_cost_cny": 10,
                    "weight_g": 300,
                }
            )

    def test_workbook_example_matches_cached_results(self) -> None:
        result = calculate_pricing(
            {
                "sale_price_cny": 48,
                "purchase_cost_cny": 7.8,
                "domestic_misc_cny": 0,
                "commission_percent": 9,
                "return_loss_percent": 10,
                "withdrawal_percent": 1.2,
                "advertising_percent": 10,
                "last_mile_percent": 2,
                "target_margin_percent": 30,
                "weight_g": 300,
                "length_cm": 24,
                "width_cm": 16,
                "height_cm": 20,
                "cny_to_rub_rate": 12.6,
                "logistics_mode": "OGL",
                "postal_transport": "空运",
            }
        )

        self.assertEqual(result["channel"], "Extra Small")
        self.assertEqual(result["fees"]["first_mile_logistics"], "10.31")
        self.assertEqual(result["fees"]["last_mile_delivery"], "1.19")
        self.assertEqual(result["total_cost_cny"], "33.80")
        self.assertEqual(result["unit_profit_cny"], "14.20")
        self.assertEqual(result["sales_margin_percent"], "29.59")
        self.assertEqual(result["break_even_price_cny"], "26.71")
        self.assertEqual(result["suggested_price_cny"], "47.91")

    def test_ogl_big_uses_dimensional_weight(self) -> None:
        result = calculate_pricing(
            {
                "sale_price_cny": 200,
                "purchase_cost_cny": 20,
                "weight_g": 2500,
                "length_cm": 60,
                "width_cm": 40,
                "height_cm": 30,
                "cny_to_rub_rate": 12.6,
                "logistics_mode": "OGL",
            }
        )

        self.assertEqual(result["channel"], "Big")
        self.assertEqual(result["charge_weight_g"], "6000.00")
        self.assertEqual(result["fees"]["first_mile_logistics"], "133.22")

    def test_postal_limits_are_enforced(self) -> None:
        with self.assertRaisesRegex(PricingError, "限重 20kg"):
            calculate_pricing(
                {
                    "sale_price_cny": 100,
                    "purchase_cost_cny": 10,
                    "weight_g": 21000,
                    "cny_to_rub_rate": 12.6,
                    "logistics_mode": "邮政",
                    "postal_transport": "陆运",
                }
            )

    def test_target_price_stabilizes_after_ogl_value_tier_change(self) -> None:
        result = calculate_target_pricing(
            {
                "purchase_cost_cny": 80,
                "weight_g": 400,
                "length_cm": 20,
                "width_cm": 20,
                "height_cm": 20,
                "cny_to_rub_rate": 12.6,
                "logistics_mode": "OGL",
            }
        )

        self.assertEqual(result["channel"], "Small")
        self.assertGreater(float(result["suggested_price_cny"]), 1500 / 12.6)

    def test_impossible_target_margin_is_rejected(self) -> None:
        with self.assertRaisesRegex(PricingError, "目标利润率合计"):
            calculate_pricing(
                {
                    "sale_price_cny": 48,
                    "purchase_cost_cny": 8,
                    "weight_g": 300,
                    "commission_percent": 20,
                    "return_loss_percent": 10,
                    "withdrawal_percent": 5,
                    "advertising_percent": 10,
                    "last_mile_percent": 5,
                    "target_margin_percent": 50,
                }
            )


if __name__ == "__main__":
    unittest.main()
