from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ozon_skill.upload.assembler import AssemblyResult
from ozon_skill.upload.config import StoreConfig
from ozon_skill.upload.log import UploadLog
from ozon_skill.upload.workflow import run_upload_workflow


class _ImportClient:
    def __init__(self, *, error: Exception | None = None, response: dict | None = None):
        self.error = error
        self.response = response or {}
        self.product_import_calls = 0

    def product_import(self, payload: dict) -> dict:
        self.product_import_calls += 1
        if self.error is not None:
            raise self.error
        return self.response


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
