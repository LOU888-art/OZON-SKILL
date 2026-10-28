from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PipelineError, PipelineService


class BrowserCollectorTests(unittest.TestCase):
    def test_browser_capture_creates_auditable_stage_one_without_alphashop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            result = service.ingest_1688_browser_capture(
                {
                    "collector_version": "0.1.0",
                    "product": {
                        "source_offer_id": "628680616884",
                        "product_url": "https://detail.1688.com/offer/628680616884.html",
                        "title_zh": "测试收纳用品",
                        "images": [
                            {"url": "https://cbu01.alicdn.com/main.jpg"}
                        ],
                        "detail_images": [
                            {"url": "https://cbu01.alicdn.com/detail.jpg"}
                        ],
                        "sku_list": [
                            {
                                "sku_id": "sku-red",
                                "specification_zh": [
                                    {"name": "颜色", "value": "红色"}
                                ],
                                "purchase_price_cny": "12.80",
                                "stock": 9,
                                "image_url": "https://cbu01.alicdn.com/red.jpg",
                            }
                        ],
                        "attributes_zh": {"材质": "塑料", "品牌": "不采集"},
                        "price_ranges": [
                            {
                                "price_cny": "12.80",
                                "minimum_quantity": 1,
                                "text_zh": "1件起",
                            }
                        ],
                        "shipping_info": {
                            "freight_cny": "3",
                            "freight_text_zh": "运费 ¥3起",
                            "ship_from_zh": "浙江金华",
                            "destination_zh": "河南郑州",
                        },
                        "capture_diagnostics": {
                            "main_candidate_count": 6,
                            "intercepted_response_count": 3,
                            "visible_sku_option_count": 4,
                        },
                        "category_path_zh": ["家居百货", "收纳用品"],
                        "supplier_info": {"name": "测试供应商"},
                    },
                },
                actor="operator",
            )

            artifact = json.loads(
                (
                    Path(temporary)
                    / "628680616884"
                    / "01_raw.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["collection_source"], "BROWSER_EXTENSION")
            self.assertEqual(result["sku_count"], 1)
            self.assertEqual(artifact["sku_list"][0]["purchase_price_cny"], "12.80")
            self.assertEqual(artifact["browser_capture"]["actor"], "operator")
            self.assertEqual(artifact["category_id"], "browser-capture-unresolved")
            self.assertNotIn("品牌", artifact["attributes_zh"])
            self.assertEqual(artifact["shipping_info"]["freight_cny"], "3")
            self.assertEqual(artifact["shipping_info"]["ship_from_zh"], "浙江金华")
            self.assertEqual(artifact["price_ranges"][0]["minimum_quantity"], 1)
            self.assertEqual(
                artifact["browser_capture"]["diagnostics"]["intercepted_response_count"],
                3,
            )

    def test_capture_rejects_missing_images_or_skus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "主图"):
                service.ingest_1688_browser_capture(
                    {
                        "source_offer_id": "628680616884",
                        "title_zh": "测试商品",
                        "images": [],
                        "sku_list": [{"sku_id": "one"}],
                    }
                )

    def test_capture_rejects_incomplete_sku_prices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "1个SKU缺少采购价"):
                service.ingest_1688_browser_capture(
                    {
                        "source_offer_id": "628680616884",
                        "title_zh": "测试商品",
                        "images": [{"url": "https://cbu01.alicdn.com/main.jpg"}],
                        "sku_list": [{"sku_id": "one", "purchase_price_cny": None}],
                    }
                )


if __name__ == "__main__":
    unittest.main()
