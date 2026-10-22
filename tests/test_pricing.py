from __future__ import annotations

import unittest

from ozon_skill.erp.pricing import (
    PricingError,
    calculate_pricing,
    calculate_target_pricing,
)


class PricingWorkbookParityTests(unittest.TestCase):
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
