from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PipelineError, PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ImageSelectionTests(unittest.TestCase):
    def _service(self, root: Path) -> PipelineService:
        pipeline_id = "1089571777226"
        _write(
            root / pipeline_id / "pipeline.json",
            {
                "pipeline_id": pipeline_id,
                "stages": {
                    "1": {"status": "PASS", "file": "01_raw.json"},
                    "3": {"status": "PASS"},
                    "4": {"status": "PASS"},
                    "5": {"status": "PASS"},
                },
            },
        )
        _write(
            root / pipeline_id / "01_raw.json",
            {
                "source_offer_id": pipeline_id,
                "images": [{"url": "https://img.example/main.jpg"}],
                "sku_list": [
                    {
                        "sku_id": "red",
                        "specification_zh": "红色",
                        "image_url": "https://img.example/shared.jpg",
                    },
                    {
                        "sku_id": "blue",
                        "specification_zh": "蓝色",
                        "image_url": "https://img.example/shared.jpg",
                    },
                ],
            },
        )
        return PipelineService(root)

    def test_shared_sku_image_can_be_saved_once_for_each_sku(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            result = service.save_image_selection(
                "1089571777226",
                [
                    {
                        "url": "https://img.example/main.jpg",
                        "action": "reference_8",
                    },
                    {
                        "url": "https://img.example/shared.jpg",
                        "sku_id": "red",
                        "action": "sku_main",
                    },
                    {
                        "url": "https://img.example/shared.jpg",
                        "sku_id": "blue",
                        "action": "sku_main",
                    },
                ],
            )

            sku_images = [
                item for item in result["images"] if item["source_role"] == "sku_image"
            ]
            reopened = service.list_images("1089571777226")

        self.assertEqual([item["action"] for item in sku_images], ["sku_main", "sku_main"])
        self.assertEqual(reopened["estimated_ai_calls"], 10)

    def test_cached_url_only_client_matches_shared_skus_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            result = service.save_image_selection(
                "1089571777226",
                [
                    {
                        "url": "https://img.example/main.jpg",
                        "action": "reference_8",
                    },
                    {
                        "url": "https://img.example/shared.jpg",
                        "action": "sku_main",
                    },
                    {
                        "url": "https://img.example/shared.jpg",
                        "action": "sku_main",
                    },
                ],
            )

        self.assertEqual(result["selected_count"], 3)

    def test_duplicate_regular_image_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "同一图片不能配置多次"):
                service.save_image_selection(
                    "1089571777226",
                    [
                        {
                            "url": "https://img.example/main.jpg",
                            "action": "reference_8",
                        },
                        {
                            "url": "https://img.example/main.jpg",
                            "action": "translate",
                        },
                    ],
                )


if __name__ == "__main__":
    unittest.main()
