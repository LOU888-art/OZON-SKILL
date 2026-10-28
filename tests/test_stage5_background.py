from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.service import PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class StageFiveBackgroundTests(unittest.TestCase):
    def test_start_stage_five_returns_immediately_and_records_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline_id = "1041747130357"
            _write(
                root / pipeline_id / "pipeline.json",
                {
                    "pipeline_id": pipeline_id,
                    "stages": {"4": {"status": "PASS", "failures": []}},
                },
            )
            _write(root / pipeline_id / "04_payload.json", {"items": []})
            service = PipelineService(root)

            with patch("ozon_skill.erp.service.threading.Thread.start") as start:
                result = service.start_stage_5(
                    pipeline_id, request_execute=True
                )

            self.assertEqual(result["status"], "RUNNING")
            start.assert_called_once_with()
            state = service.get_pipeline(pipeline_id)
            self.assertEqual(state["stages"]["5"]["status"], "RUNNING")
            self.assertIn((pipeline_id, 5), service._background_jobs)


if __name__ == "__main__":
    unittest.main()
