from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PLAN_CATALOG, PipelineError, PipelineService
from ozon_skill.erp.stores import StoreRegistry


class _ReadOnlyClient:
    def category_tree(self, *, language: str = "RU") -> dict:
        return {"result": [], "language": language}


def _client_factory(_client_id: str, _api_key: str) -> _ReadOnlyClient:
    return _ReadOnlyClient()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class StoreSelectionTests(unittest.TestCase):
    def test_sku_can_be_excluded_restored_and_last_sku_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root / "product-1" / "pipeline.json",
                {
                    "pipeline_id": "product-1",
                    "stages": {
                        str(number): {"status": "PASS", "failures": []}
                        for number in range(1, 6)
                    },
                },
            )
            _write(
                root / "product-1" / "01_raw.json",
                {
                    "source_offer_id": "product-1",
                    "sku_list": [{"sku_id": "a"}, {"sku_id": "b"}],
                },
            )
            service = PipelineService(root)

            removed = service.set_sku_excluded(
                "product-1", "a", excluded=True, actor="owner"
            )
            state = json.loads(
                (root / "product-1" / "pipeline.json").read_text(encoding="utf-8")
            )

            self.assertEqual(removed["active_sku_ids"], ["b"])
            self.assertEqual(removed["excluded_sku_ids"], ["a"])
            self.assertEqual(state["stages"]["2"]["status"], "PASS")
            self.assertEqual(state["stages"]["3"]["status"], "PASS")
            self.assertEqual(state["stages"]["4"]["status"], "STALE")
            self.assertEqual(state["stages"]["5"]["status"], "STALE")
            with self.assertRaisesRegex(PipelineError, "至少必须保留一个"):
                service.set_sku_excluded(
                    "product-1", "b", excluded=True, actor="owner"
                )

            restored = service.set_sku_excluded(
                "product-1", "a", excluded=False, actor="owner"
            )
            self.assertEqual(restored["excluded_sku_ids"], [])
            self.assertEqual(restored["active_sku_ids"], ["a", "b"])

    def test_store_authorization_is_unlimited_for_every_plan(self) -> None:
        self.assertTrue(
            all(plan["limits"]["stores"] is None for plan in PLAN_CATALOG.values())
        )
        with tempfile.TemporaryDirectory() as temporary:
            registry = StoreRegistry(
                Path(temporary) / "stores.json", client_factory=_client_factory
            )
            for index in range(15):
                registry.authorize(
                    {
                        "store_id": f"store-{index}",
                        "name": f"Ozon Store {index}",
                        "client_id": f"client-{index}",
                        "api_key": f"secret-{index}",
                        "currency_code": "RUB",
                    }
                )
            self.assertEqual(len(registry.list_public()), 15)

    def test_bound_pipeline_rejects_a_different_store_after_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root / ".ozon-stores.json",
                {
                    "schema_version": "1.0",
                    "stores": [
                        {
                            "store_id": "store-a",
                            "name": "A",
                            "client_id": "client-a",
                            "api_key": "secret-a",
                            "currency_code": "RUB",
                        },
                        {
                            "store_id": "store-b",
                            "name": "B",
                            "client_id": "client-b",
                            "api_key": "secret-b",
                            "currency_code": "RUB",
                        },
                    ],
                },
            )
            _write(
                root / "product-1" / "pipeline.json",
                {"pipeline_id": "product-1", "store_id": "store-a", "stages": {}},
            )
            service = PipelineService(root)

            self.assertEqual(
                service._selected_store("product-1", "store-a")["store_id"],
                "store-a",
            )
            with self.assertRaisesRegex(PipelineError, "第二阶段"):
                service._selected_store("product-1", "store-b")
            self.assertEqual(
                service._selected_store(
                    "product-1", "store-b", allow_rebind=True
                )["store_id"],
                "store-b",
            )

    def test_changing_store_invalidates_mapping_and_all_downstream_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root / ".ozon-stores.json",
                {
                    "schema_version": "1.0",
                    "stores": [
                        {
                            "store_id": store_id,
                            "name": store_id,
                            "client_id": store_id,
                            "api_key": "secret",
                            "currency_code": "RUB",
                        }
                        for store_id in ("store-a", "store-b")
                    ],
                },
            )
            _write(
                root / "product-1" / "pipeline.json",
                {
                    "pipeline_id": "product-1",
                    "store_id": "store-a",
                    "stages": {
                        str(number): {"status": "PASS", "failures": []}
                        for number in range(1, 6)
                    },
                },
            )
            service = PipelineService(root)
            current = service.product_settings("product-1")
            changed = {**current, "store_id": "store-b"}

            service._apply_product_settings(
                "product-1",
                changed,
                actor="owner",
                note="更换目标店铺",
                source="TEST",
            )
            state = json.loads(
                (root / "product-1" / "pipeline.json").read_text(encoding="utf-8")
            )

            self.assertEqual(state["store_id"], "store-b")
            self.assertEqual(state["stages"]["1"]["status"], "PASS")
            for number in range(2, 6):
                self.assertEqual(state["stages"][str(number)]["status"], "STALE")
                self.assertTrue(
                    state["stages"][str(number)]["stale_after_store_change"]
                )


if __name__ == "__main__":
    unittest.main()
