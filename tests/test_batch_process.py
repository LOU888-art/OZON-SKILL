from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.service import PipelineError, PipelineService


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class BatchProcessTests(unittest.TestCase):
    def _service(self, root: Path) -> PipelineService:
        _write(
            root / ".ozon-stores.json",
            {
                "schema_version": "1.0",
                "stores": [
                    {
                        "store_id": "111",
                        "name": "Ozon Store",
                        "client_id": "client",
                        "api_key": "secret",
                        "currency_code": "RUB",
                    }
                ],
            },
        )
        for pipeline_id in ("628680616884", "988960243766"):
            _write(
                root / pipeline_id / "pipeline.json",
                {
                    "pipeline_id": pipeline_id,
                    "created_at": "2026-10-23T00:00:00Z",
                    "updated_at": "2026-10-23T00:00:00Z",
                    "stages": {"1": {"status": "PASS", "failures": []}},
                },
            )
            _write(
                root / pipeline_id / "01_raw.json",
                {"title_zh": "测试商品", "sku_list": [{"sku_id": "sku"}]},
            )
        return PipelineService(root)

    def _payload(self) -> dict:
        return {
            "pipeline_ids": ["628680616884", "988960243766"],
            "target_stage": 4,
            "store_id": "111",
            "confidence_threshold": 0.7,
            "media_mode": "image_plan",
            "text_mode": "ai_generate",
            "store_config": {
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
            },
            "confirm_external_requests": True,
        }

    def test_batch_process_isolates_failures_and_never_runs_stage_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            with patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_batch_process(self._payload(), "QSM")

            def stage_three(pipeline_id: str, **_kwargs):
                if pipeline_id == "988960243766":
                    return {"status": "FAIL", "failures": ["AI 通道暂不可用"]}
                return {"status": "PASS", "failures": []}

            with (
                patch.object(service, "run_stage_2", return_value={"status": "PASS", "failures": []}) as stage2,
                patch.object(service, "run_stage_3", side_effect=stage_three) as stage3,
                patch.object(service, "run_stage_4", return_value={"status": "PASS", "failures": []}) as stage4,
                patch.object(service, "run_stage_5") as stage5,
            ):
                service._run_batch_process(job["job_id"])

            result = service.batch_process_status(job["job_id"])

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["passed_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(stage2.call_count, 2)
        self.assertEqual(stage3.call_count, 2)
        self.assertEqual(stage4.call_count, 1)
        stage5.assert_not_called()
        self.assertIn("未执行真实上架", result["message"])

    def test_batch_process_requires_explicit_cost_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            payload = self._payload()
            payload["confirm_external_requests"] = False
            with self.assertRaisesRegex(PipelineError, "确认"):
                service.start_batch_process(payload, "QSM")

    def test_batch_job_whitelists_persisted_store_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service(Path(temporary))
            payload = self._payload()
            payload["pipeline_ids"] = ["628680616884"]
            payload["store_config"]["api_key"] = "must-not-be-persisted"
            payload["store_config"]["attribute_overrides"] = {"85": {"value": "bad"}}
            with patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_batch_process(payload, "QSM")

        self.assertNotIn("must-not-be-persisted", str(job))
        self.assertEqual(job["store_config"]["attribute_overrides"], {})

    def test_restart_interrupts_batch_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self._service(root)
            with patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_batch_process(self._payload(), "QSM")

            restarted = PipelineService(root)
            result = restarted.batch_process_status(job["job_id"])

        self.assertEqual(result["status"], "INTERRUPTED")
        self.assertTrue(
            all(item["status"] == "INTERRUPTED" for item in result["items"])
        )
        self.assertIn("避免重复调用付费接口", result["message"])


if __name__ == "__main__":
    unittest.main()
