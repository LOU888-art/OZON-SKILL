from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.assets import managed_asset_id
from ozon_skill.erp.service import PipelineError, PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class AssetLibraryTests(unittest.TestCase):
    def _pipeline(self, root: Path, *, published: bool = False) -> None:
        directory = root / "628680616884"
        _write(
            directory / "pipeline.json",
            {
                "pipeline_id": "628680616884",
                "created_at": "2026-10-23T00:00:00Z",
                "updated_at": "2026-10-23T00:00:00Z",
                "stages": {
                    "1": {"status": "PASS"},
                    "3": {"status": "PASS"},
                    "5": {"status": "PASS" if published else "LOCKED"},
                },
            },
        )
        _write(
            directory / "01_raw.json",
            {"images": [{"url": "https://images.example.com/source.png"}]},
        )
        _write(
            directory / "03_final.json",
            {
                "localized_images": [
                    {
                        "source_url": "https://images.example.com/source.png",
                        "final_public_url": "https://cdn.example.com/assets/northstar/628680616884/current.png",
                        "storage_object_key": "northstar/628680616884/current.png",
                        "storage_sha256": "abc",
                        "storage_size_bytes": 120,
                        "plan_action": "reference_8",
                    }
                ]
            },
        )

    def test_inventory_combines_sources_managed_assets_and_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pipeline(root)
            service = PipelineService(root)
            service.assets.upsert(
                {
                    "object_key": "northstar/old/orphan.png",
                    "public_url": "https://cdn.example.com/assets/northstar/old/orphan.png",
                    "size_bytes": 80,
                    "asset_type": "translated",
                }
            )
            inventory = service.asset_library()

        self.assertEqual(inventory["summary"]["total"], 3)
        self.assertEqual(inventory["summary"]["managed"], 2)
        self.assertEqual(inventory["summary"]["source"], 1)
        self.assertEqual(inventory["summary"]["orphaned"], 1)
        self.assertEqual(inventory["summary"]["managed_bytes"], 200)
        orphan = next(item for item in inventory["assets"] if item.get("orphaned"))
        current = next(
            item
            for item in inventory["assets"]
            if item.get("object_key") == "northstar/628680616884/current.png"
        )
        self.assertTrue(orphan["can_delete"])
        self.assertFalse(current["can_delete"])
        self.assertEqual(current["reference_count"], 1)

    def test_delete_only_removes_unreferenced_never_published_object(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.deleted = []

            def delete(self, key: str) -> None:
                self.deleted.append(key)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pipeline(root)
            service = PipelineService(root)
            orphan = service.assets.upsert(
                {
                    "object_key": "northstar/old/orphan.png",
                    "public_url": "https://cdn.example.com/assets/northstar/old/orphan.png",
                    "size_bytes": 80,
                }
            )
            client = _Client()
            with patch.object(service.object_storage, "client", return_value=client):
                result = service.delete_asset(orphan["asset_id"])

            self.assertEqual(client.deleted, ["northstar/old/orphan.png"])
            self.assertIsNone(service.assets.get(orphan["asset_id"]))
            current_id = managed_asset_id(
                "northstar/628680616884/current.png",
                "https://cdn.example.com/assets/northstar/628680616884/current.png",
            )
            with self.assertRaisesRegex(PipelineError, "仍被商品任务引用"):
                service.delete_asset(current_id)
            self.assertEqual(result["summary"]["orphaned"], 0)

    def test_published_protection_is_sticky(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pipeline(root, published=True)
            service = PipelineService(root)
            first = service.asset_library()
            managed = next(item for item in first["assets"] if item.get("managed"))
            self.assertTrue(managed["published"])

            # Even if later artifacts no longer reference it, publication history remains.
            (root / "628680616884" / "03_final.json").unlink()
            second = service.asset_library()
            protected = next(
                item for item in second["assets"] if item["asset_id"] == managed["asset_id"]
            )
            self.assertTrue(protected["published"])
            self.assertTrue(protected["orphaned"])
            self.assertFalse(protected["can_delete"])

    def test_delete_rejects_asset_from_another_bucket(self) -> None:
        class _Client:
            bucket = "new-bucket"
            endpoint = "oss-cn-hongkong.aliyuncs.com"

            def delete(self, _key: str) -> None:
                raise AssertionError("delete must not be called")

        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            orphan = service.assets.upsert(
                {
                    "object_key": "northstar/old/orphan.png",
                    "public_url": "https://old.example.com/northstar/old/orphan.png",
                    "storage_bucket": "old-bucket",
                    "storage_endpoint": "oss-cn-hongkong.aliyuncs.com",
                }
            )
            with patch.object(service.object_storage, "client", return_value=_Client()):
                with self.assertRaisesRegex(PipelineError, "历史 OSS Bucket"):
                    service.delete_asset(orphan["asset_id"])


if __name__ == "__main__":
    unittest.main()
