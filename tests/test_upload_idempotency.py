from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ozon_skill.upload.assembler import AssemblyResult
from ozon_skill.upload.config import StoreConfig
from ozon_skill.upload.log import UploadLog
from ozon_skill.upload.workflow import run_upload_workflow


class _ImportClient:
    def __init__(self, *, error: Exception | None = None, response: dict | None = None):
        self.error = error
        self.response = response or {}
        self.product_import_calls = 0
        self.import_info_calls = 0

    def product_import(self, payload: dict) -> dict:
        self.product_import_calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    def import_info(self, task_id: int) -> dict:
        self.import_info_calls += 1
        return {"result": {"items": []}}


def _assembly(*, marker: str = "first") -> AssemblyResult:
    return AssemblyResult(
        payload={
            "items": [
                {
                    "offer_id": "QSM102401",
                    "name": marker,
                    "attributes": [],
                    "images": [],
                }
            ]
        },
        offer_ids=["QSM102401"],
        validation_errors=[],
        warnings=[],
        dimension_source="test",
        required_attribute_ids=[],
    )


def _store() -> StoreConfig:
    return StoreConfig(
        store_id="store-1",
        credential_prefix="STORE_1",
        cny_to_rub_rate=Decimal("12.6"),
    )


class UploadIdempotencyTests(unittest.TestCase):
    def test_workflow_deadline_stops_polling_and_preserves_task_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload.json"
            client = _ImportClient(response={"result": {"task_id": 12345}})

            with patch(
                "ozon_skill.upload.workflow.time.monotonic",
                side_effect=[0.0, 500.0],
            ):
                result = run_upload_workflow(
                    store=_store(),
                    assembly=_assembly(),
                    upload_log=UploadLog(path),
                    execute=True,
                    client=client,
                    deadline_seconds=420,
                )

            self.assertEqual(result["status"], "WORKFLOW_TIMEOUT")
            self.assertEqual(result["task_id"], 12345)
            self.assertTrue(result["request_sent"])
            self.assertEqual(client.product_import_calls, 1)
            self.assertEqual(client.import_info_calls, 0)
            self.assertIn("不会重复创建商品", result["error"])

    def test_connection_error_is_persisted_and_exact_retry_is_not_resubmitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload.json"
            client = _ImportClient(error=TimeoutError("response lost"))

            first = run_upload_workflow(
                store=_store(),
                assembly=_assembly(),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
            )
            second = run_upload_workflow(
                store=_store(),
                assembly=_assembly(),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
            )

            self.assertEqual(first["status"], "SUBMISSION_OUTCOME_UNKNOWN")
            self.assertEqual(client.product_import_calls, 1)
            self.assertTrue(second["reused_existing_result"])
            self.assertTrue(second["manual_reconciliation_required"])
            self.assertEqual(len(UploadLog(path).data["entries"]), 1)

    def test_missing_task_id_is_persisted_and_exact_retry_is_not_resubmitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload.json"
            client = _ImportClient(response={"result": {}})

            first = run_upload_workflow(
                store=_store(),
                assembly=_assembly(),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
            )
            second = run_upload_workflow(
                store=_store(),
                assembly=_assembly(),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
            )

            self.assertEqual(first["status"], "IMPORT_RESPONSE_MISSING_TASK_ID")
            self.assertEqual(client.product_import_calls, 1)
            self.assertTrue(second["manual_reconciliation_required"])
            self.assertEqual(len(UploadLog(path).data["entries"]), 1)

    def test_changed_payload_with_same_offer_is_blocked_after_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload.json"
            client = _ImportClient(error=ConnectionError("connection reset"))

            run_upload_workflow(
                store=_store(),
                assembly=_assembly(),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
            )
            changed = run_upload_workflow(
                store=_store(),
                assembly=_assembly(marker="changed"),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
            )

            self.assertEqual(changed["status"], "SKIPPED_EXISTING_TASK")
            self.assertEqual(changed["duplicate_offer_ids"], ["QSM102401"])
            self.assertEqual(client.product_import_calls, 1)


if __name__ == "__main__":
    unittest.main()
