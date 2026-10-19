import csv
import tempfile
import unittest
from pathlib import Path

from scripts.ogl_pricing import calculate, run


class OGLPricingTests(unittest.TestCase):
    def sample(self, **updates):
        row = {
            "product_name": "Example",
            "sale_price_cny": "62",
            "purchase_cost_cny": "13",
            "domestic_misc_cny": "0",
            "return_rate_pct": "10",
            "withdrawal_rate_pct": "1.2",
            "ads_rate_pct": "0",
            "last_mile_rate_pct": "2",
            "commission_rate_pct": "9",
            "weight_g": "450",
            "length_cm": "0",
            "width_cm": "0",
            "height_cm": "0",
            "exchange_rate_rub_per_cny": "12.6",
            "target_margin_pct": "30",
            "supplier_url": "",
            "product_facts": "",
        }
        row.update({key: str(value) for key, value in updates.items()})
        return row

    def test_matches_supplied_workbook_example(self):
        result = calculate(self.sample())
        self.assertEqual(result["ogl_channel"], "Extra Small")
        self.assertEqual(result["first_mile_cny"], "14.02")
        self.assertEqual(result["total_cost_cny"], "40.78")
        self.assertEqual(result["profit_cny"], "21.22")
        self.assertEqual(result["sales_margin_pct"], "34.23")
        self.assertEqual(result["break_even_price_cny"], "34.72")
        self.assertEqual(result["suggested_price_cny"], "56.52")

    def test_all_ogl_bands(self):
        cases = [
            ({"sale_price_cny": 100, "weight_g": 600}, "Budget"),
            ({"sale_price_cny": 200, "weight_g": 1000}, "Small"),
            ({"sale_price_cny": 200, "weight_g": 3000}, "Big"),
            ({"sale_price_cny": 600, "weight_g": 3000}, "Premium Small"),
            ({"sale_price_cny": 600, "weight_g": 6000}, "Premium Big"),
        ]
        for updates, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(calculate(self.sample(**updates))["ogl_channel"], expected)

    def test_percentage_string(self):
        result = calculate(self.sample(commission_rate_pct="9%"))
        self.assertEqual(result["commission_cny"], "5.58")

    def test_csv_round_trip(self):
        row = self.sample()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            output = Path(directory) / "output.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(run(source, output), 1)
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                result = next(csv.DictReader(handle))
            self.assertEqual(result["profit_cny"], "21.22")


if __name__ == "__main__":
    unittest.main()
