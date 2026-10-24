from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ozon_skill.erp.service import PipelineError, PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class LocalDeletionTests(unittest.TestCase):
    def test_bulk_pipeline_delete_is_local_and_blocks_running_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pipeline_id, status in (("ready", "PASS"), ("running", "RUNNING")):
                _write(
                    root / pipeline_id / "pipeline.json",
                    {
                        "pipeline_id": pipeline_id,
                        "stages": {"1": {"status": status}},
                    },
                )
            service = PipelineService(root)
            running_state = json.loads(
                (root / "running" / "pipeline.json").read_text(encoding="utf-8")
            )
            running_state["stages"]["1"]["status"] = "RUNNING"
            _write(root / "running" / "pipeline.json", running_state)

            result = service.delete_pipelines(
                {
                    "pipeline_ids": ["ready", "running"],
                    "confirm_delete": True,
                },
                "admin",
            )

            self.assertEqual(result["deleted"], ["ready"])
            self.assertEqual(result["blocked"][0]["pipeline_id"], "running")
            self.assertTrue(result["local_only"])
            self.assertFalse(result["ozon_products_deleted"])
            self.assertFalse((root / "ready").exists())
            self.assertTrue((root / "running").exists())

    def test_pipeline_delete_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "确认删除"):
                service.delete_pipelines({"pipeline_ids": ["one"]})

    def test_bulk_candidate_delete_reports_missing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(
                root / ".market-research.json",
                {
                    "schema_version": "1.0",
                    "rules": None,
                    "candidates": {
                        "candidate-1": {
                            "candidate_id": "candidate-1",
                            "decision": "NEW",
                        }
                    },
                },
            )
            service = PipelineService(root)

            result = service.delete_market_candidates(
                {
                    "candidate_ids": ["candidate-1", "missing"],
                    "confirm_delete": True,
                }
            )

            self.assertEqual(result["deleted"], ["candidate-1"])
            self.assertEqual(result["missing"], ["missing"])
            self.assertEqual(result["research"]["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
