from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ProductCatalogTests(unittest.TestCase):
    def _product(
        self,
        root: Path,
        pipeline_id: str,
        statuses: dict[str, str],
        *,
        updated_at: str,
    ) -> None:
        directory = root / pipeline_id
        _write(
            directory / "pipeline.json",
            {
                "pipeline_id": pipeline_id,
                "created_at": "2026-10-20T00:00:00Z",
                "updated_at": updated_at,
                "store_id": "111",
                "stages": {
                    number: {
                        "status": status,
                        "failures": ["图片地址不可访问"] if status == "FAIL" else [],
                    }
                    for number, status in statuses.items()
                },
            },
        )
        _write(
            directory / "01_raw.json",
            {
                "title_zh": f"中文商品 {pipeline_id}",
                "images": [{"url": f"https://images.example.com/{pipeline_id}.png"}],
                "sku_list": [{"sku_id": "a"}, {"sku_id": "b"}],
            },
        )
        if statuses.get("3") in {"PASS", "STALE"}:
            _write(
                directory / "03_final.json",
                {
                    "title_ru": f"Русский товар {pipeline_id}",
                    "ozon_category_id": 170000,
                    "ozon_type_id": 98000,
                    "localized_images": [
                        {"final_public_url": f"https://cdn.example.com/{pipeline_id}.png"}
                    ],
                },
            )
        if statuses.get("4") in {"PASS", "STALE"}:
            _write(
                directory / "04_payload.json",
                {
                    "items": [
                        {
                            "offer_id": f"111-{pipeline_id}-a",
                            "name": "Карточка",
                            "price": "1299.90",
                            "currency_code": "RUB",
                        },
                        {
                            "offer_id": f"111-{pipeline_id}-b",
                            "name": "Карточка",
                            "price": "1499.90",
                            "currency_code": "RUB",
                        },
                    ]
                },
            )

    def test_catalog_summarizes_products_skus_and_prices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._product(
                root,
                "628680616884",
                {str(number): "PASS" for number in range(1, 6)},
                updated_at="2026-10-23T02:00:00Z",
            )
            self._product(
                root,
                "988960243766",
                {"1": "PASS", "2": "FAIL"},
                updated_at="2026-10-23T03:00:00Z",
            )
            catalog = PipelineService(root).product_catalog()

        self.assertEqual(catalog["summary"]["total"], 2)
        self.assertEqual(catalog["summary"]["published"], 1)
        self.assertEqual(catalog["summary"]["failed"], 1)
        self.assertEqual(catalog["summary"]["sku_count"], 4)
        self.assertEqual(catalog["products"][0]["pipeline_id"], "988960243766")
        published = next(row for row in catalog["products"] if row["status"] == "published")
        self.assertEqual(published["price_min"], "1299.90")
        self.assertEqual(published["price_max"], "1499.90")
        self.assertEqual(published["currency_code"], "RUB")
        self.assertEqual(published["sku_count"], 2)
        self.assertTrue(published["thumbnail_url"].startswith("https://cdn.example.com/"))

    def test_stale_published_product_requires_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._product(
                root,
                "628680616884",
                {"1": "PASS", "2": "PASS", "3": "PASS", "4": "PASS", "5": "STALE"},
                updated_at="2026-10-23T04:00:00Z",
            )
            row = PipelineService(root).product_catalog()["products"][0]

        self.assertEqual(row["status"], "needs_update")
        self.assertEqual(row["current_stage"], 5)


if __name__ == "__main__":
    unittest.main()
