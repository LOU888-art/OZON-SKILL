from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.service import PipelineError, PipelineService


class BatchImportTests(unittest.TestCase):
    def test_batch_import_deduplicates_and_isolates_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_batch_import(
                    {
                        "items": "\n".join(
                            [
                                "https://detail.1688.com/offer/628680616884.html",
                                "628680616884",
                                "988960243766",
                                "not-a-product",
                            ]
                        ),
                        "confirm_external_requests": True,
                    },
                    "QSM",
                )

            def collect(pipeline_id: str):
                if pipeline_id == "988960243766":
                    raise PipelineError("AlphaShop 临时失败", status=502)
                return {"status": "PASS", "failures": []}

            with patch.object(service, "run_stage_1", side_effect=collect):
                service._run_batch_import(job["job_id"])
            result = service.batch_import_status(job["job_id"])

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["passed_count"], 1)
        self.assertEqual(result["failed_count"], 2)
        self.assertEqual(result["completed_count"], 3)
        statuses = {
            item.get("pipeline_id") or item.get("input"): item["status"]
            for item in result["items"]
        }
        self.assertEqual(statuses["628680616884"], "PASS")
        self.assertEqual(statuses["988960243766"], "FAIL")
        self.assertEqual(statuses["not-a-product"], "FAIL")

    def test_batch_import_requires_explicit_external_api_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "AlphaShop"):
                service.start_batch_import(
                    {"items": ["628680616884"]}, "QSM"
                )

    def test_restart_marks_queued_job_interrupted_without_replaying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = PipelineService(root)
            with patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_batch_import(
                    {
                        "items": ["628680616884"],
                        "confirm_external_requests": True,
                    },
                    "QSM",
                )

            restarted = PipelineService(root)
            result = restarted.batch_import_status(job["job_id"])

        self.assertEqual(result["status"], "INTERRUPTED")
        self.assertEqual(result["items"][0]["status"], "INTERRUPTED")
        self.assertIn("避免重复扣费", result["message"])

    def test_batch_import_limits_request_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "100"):
                service.start_batch_import(
                    {
                        "items": [str(100000000000 + index) for index in range(101)],
                        "confirm_external_requests": True,
                    },
                    "QSM",
                )


if __name__ == "__main__":
    unittest.main()
