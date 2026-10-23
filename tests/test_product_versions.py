from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PipelineError, PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ProductVersionTests(unittest.TestCase):
    def _service(self, root: Path, pipeline_id: str = "123456") -> PipelineService:
        _write(
            root / pipeline_id / "pipeline.json",
            {
                "pipeline_id": pipeline_id,
                "created_at": "2026-10-23T00:00:00Z",
                "updated_at": "2026-10-23T00:00:00Z",
                "store_id": "store-a",
                "stages": {
                    str(number): {
                        "status": "PASS",
                        "file": f"0{number}.json",
                        "failures": [],
                    }
                    for number in range(1, 6)
                },
            },
        )
        return PipelineService(root)

    def test_batch_edit_creates_versions_and_marks_payload_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self._service(root)
            result = service.batch_edit_products(
                {
                    "pipeline_ids": ["123456"],
                    "patch": {
                        "cny_to_rub_rate": "13.2",
                        "vat": "0.2",
                        "dimension_defaults": {
                            "depth": 100,
                            "width": 90,
                            "height": 200,
                            "weight": 450,
                            "dimension_unit": "mm",
                            "weight_unit": "g",
                        },
                    },
                    "note": "旺季参数",
                },
                "operator",
            )

            state = json.loads((root / "123456" / "pipeline.json").read_text())
            history = service.product_versions("123456")

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(state["stages"]["4"]["status"], "STALE")
        self.assertEqual(state["stages"]["5"]["status"], "STALE")
        self.assertEqual(history["current"]["cny_to_rub_rate"], "13.2")
        self.assertEqual(history["current"]["vat"], "0.2")
        self.assertEqual(len(history["versions"]), 2)
        self.assertEqual(history["versions"][0]["actor"], "operator")

    def test_rollback_is_a_new_version_and_does_not_run_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self._service(root)
            service.batch_edit_products(
                {
                    "pipeline_ids": ["123456"],
                    "patch": {"cny_to_rub_rate": "13.2"},
                },
                "operator",
            )
            versions = service.product_versions("123456")["versions"]
            baseline = versions[-1]
            result = service.restore_product_version(
                "123456", baseline["version_id"], "reviewer"
            )
            history = service.product_versions("123456")

        self.assertTrue(result["changed"])
        self.assertEqual(history["current"]["cny_to_rub_rate"], "")
        self.assertEqual(history["versions"][0]["source"], "ROLLBACK")
        self.assertEqual(len(history["versions"]), 3)

    def test_batch_edit_rejects_secrets_and_product_specific_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            for forbidden in ("api_key", "attribute_overrides", "sku_price_overrides"):
                with self.subTest(field=forbidden):
                    with self.assertRaises(PipelineError):
                        service.batch_edit_products(
                            {
                                "pipeline_ids": ["123456"],
                                "patch": {forbidden: {"x": "secret"}},
                            },
                            "operator",
                        )

    def test_unchanged_edit_does_not_create_empty_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            result = service.batch_edit_products(
                {
                    "pipeline_ids": ["123456"],
                    "patch": {"vat": "0"},
                },
                "operator",
            )

            history = service.product_versions("123456")

        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(history["versions"], [])

    def test_batch_edit_is_limited_to_fifty_products(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaises(PipelineError):
                service.batch_edit_products(
                    {
                        "pipeline_ids": [str(index) for index in range(51)],
                        "patch": {"vat": "0"},
                    },
                    "operator",
                )


if __name__ == "__main__":
    unittest.main()
