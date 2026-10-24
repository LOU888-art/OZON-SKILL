from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class StageFourPreflightTests(unittest.TestCase):
    def test_all_invalid_sku_prices_are_reported_in_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline_id = "16880001"
            directory = root / pipeline_id
            _write(
                root / ".ozon-stores.json",
                {
                    "schema_version": "1.0",
                    "stores": [
                        {
                            "store_id": "store-1",
                            "name": "Test",
                            "client_id": "client",
                            "api_key": "secret",
                            "currency_code": "RUB",
                        }
                    ],
                },
            )
            _write(
                directory / "pipeline.json",
                {
                    "pipeline_id": pipeline_id,
                    "stages": {
                        str(stage): {"status": "PASS", "failures": []}
                        for stage in range(1, 4)
                    },
                },
            )
            _write(
                directory / "01_raw.json",
                {
                    "source_offer_id": pipeline_id,
                    "sku_list": [
                        {"sku_id": "sku-a", "purchase_price_cny": ""},
                        {"sku_id": "sku-b", "purchase_price_cny": "0"},
                    ],
                },
            )
            _write(directory / "02_mapped.json", {})
            _write(directory / "03_final.json", {})
            service = PipelineService(root)

            result = service.run_stage_4(
                pipeline_id,
                store_config={
                    "store_id": "store-1",
                    "cny_to_rub_rate": "12.5",
                    "dimension_defaults": {
                        "depth": 80,
                        "width": 80,
                        "height": 200,
                        "weight": 350,
                        "dimension_unit": "mm",
                        "weight_unit": "g",
                    },
                    "vat": "0",
                    "sku_price_overrides": {},
                    "attribute_overrides": {},
                },
            )

            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["preflight"]["issue_count"], 2)
            self.assertTrue(any("sku-a" in issue for issue in result["failures"]))
            self.assertTrue(any("sku-b" in issue for issue in result["failures"]))
            artifact = json.loads((directory / "04_payload.json").read_text())
            self.assertEqual(artifact["preflight"]["issue_count"], 2)
            self.assertEqual(artifact["items"], [])

    def test_all_invalid_dimensions_are_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline_id = "16880002"
            directory = root / pipeline_id
            _write(
                root / ".ozon-stores.json",
                {
                    "schema_version": "1.0",
                    "stores": [
                        {
                            "store_id": "store-1",
                            "name": "Test",
                            "client_id": "client",
                            "api_key": "secret",
                            "currency_code": "RUB",
                        }
                    ],
                },
            )
            _write(
                directory / "pipeline.json",
                {
                    "pipeline_id": pipeline_id,
                    "stages": {
                        str(stage): {"status": "PASS", "failures": []}
                        for stage in range(1, 4)
                    },
                },
            )
            _write(
                directory / "01_raw.json",
                {
                    "source_offer_id": pipeline_id,
                    "sku_list": [
                        {"sku_id": "sku-a", "purchase_price_cny": "10"}
                    ],
                },
            )
            _write(directory / "02_mapped.json", {})
            _write(directory / "03_final.json", {})
            service = PipelineService(root)

            result = service.run_stage_4(
                pipeline_id,
                store_config={
                    "store_id": "store-1",
                    "cny_to_rub_rate": "12.5",
                    "dimension_defaults": {
                        "depth": 0,
                        "width": "bad",
                        "height": -1,
                        "weight": "",
                    },
                    "sku_price_overrides": {},
                },
            )

            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["preflight"]["issue_count"], 4)
            self.assertTrue(any("长度" in issue for issue in result["failures"]))
            self.assertTrue(any("宽度" in issue for issue in result["failures"]))
            self.assertTrue(any("高度" in issue for issue in result["failures"]))
            self.assertTrue(any("重量" in issue for issue in result["failures"]))


if __name__ == "__main__":
    unittest.main()
