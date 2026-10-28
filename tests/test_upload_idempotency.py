from __future__ import annotations

import json
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
    def test_published_product_passes_when_only_rich_content_cdn_is_delayed(self) -> None:
        class _Client:
            attributes_update_calls = 0

            def product_import(self, _payload: dict) -> dict:
                return {"result": {"task_id": 123}}

            def import_info(self, _task_id: int) -> dict:
                return {
                    "result": {
                        "items": [
                            {
                                "offer_id": "QSM102401",
                                "product_id": 777,
                                "status": "imported",
                                "errors": [],
                            }
                        ]
                    }
                }

            def product_info_list(self, _offer_ids: list[str]) -> dict:
                return {"items": [{"offer_id": "QSM102401", "id": 777}]}

            def product_pictures_info(self, _product_ids: list[int]) -> dict:
                return {
                    "items": [
                        {
                            "product_id": 777,
                            "photo": None,
                            "primary_photo": None,
                            "color_photo": None,
                            "errors": [],
                        }
                    ]
                }

            def product_pictures_import(
                self, _offer_id: str, _images: list[str]
            ) -> dict:
                return {"result": {"task_id": 456}}

            def attributes_update(self, _items: list[dict]) -> dict:
                self.attributes_update_calls += 1
                return {"result": {}}

            def product_info_attributes(self, _offer_ids: list[str]) -> dict:
                return {"items": [{"offer_id": "QSM102401", "attributes": []}]}

        rich = {
            "version": 0.3,
            "content": [
                {
                    "widgetName": "raShowcase",
                    "blocks": [
                        {
                            "img": {
                                "src": "https://cdn.example.com/ai-2.jpg",
                                "srcMobile": "https://cdn.example.com/ai-2.jpg",
                            }
                        }
                    ],
                }
            ],
        }
        assembly = AssemblyResult(
            payload={
                "items": [
                    {
                        "offer_id": "QSM102401",
                        "name": "Ароматическая подвеска для шкафа и комнаты, декоративный ароматизатор для домашнего пространства",
                        "attributes": [
                            {
                                "id": 999,
                                "complex_id": 0,
                                "values": [{"value": json.dumps(rich)}],
                            }
                        ],
                        "images": [
                            f"https://cdn.example.com/ai-{index}.jpg"
                            for index in range(1, 9)
                        ],
                    }
                ]
            },
            offer_ids=["QSM102401"],
            validation_errors=[],
            warnings=[],
            dimension_source="test",
            required_attribute_ids=[],
        )
        client = _Client()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_upload_workflow(
                store=_store(),
                assembly=assembly,
                upload_log=UploadLog(Path(temporary) / "upload.json"),
                execute=True,
                client=client,
                poll_interval_seconds=0,
                image_hosting_max_polls=1,
                verification_max_polls=1,
            )

        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["rich_content_deferred"])
        self.assertTrue(result["post_publish_warnings"])
        self.assertIn("富内容已延后", result["post_publish_warnings"][0])
        self.assertEqual(client.attributes_update_calls, 0)

    def test_deferred_rich_content_can_be_completed_without_reimporting_product(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.product_import_calls = 0
                self.picture_info_calls = 0
                self.rich_update_calls = 0

            def product_import(self, _payload: dict) -> dict:
                self.product_import_calls += 1
                return {"result": {"task_id": 123}}

            def import_info(self, _task_id: int) -> dict:
                return {
                    "result": {
                        "items": [
                            {
                                "offer_id": "QSM102401",
                                "product_id": 777,
                                "status": "imported",
                                "errors": [],
                            }
                        ]
                    }
                }

            def product_info_list(self, _offer_ids: list[str]) -> dict:
                return {"items": [{"offer_id": "QSM102401", "id": 777}]}

            def product_pictures_info(self, _product_ids: list[int]) -> dict:
                self.picture_info_calls += 1
                if self.picture_info_calls == 1:
                    return {
                        "items": [
                            {
                                "product_id": 777,
                                "photo": None,
                                "errors": [],
                            }
                        ]
                    }
                return {
                    "items": [
                        {
                            "product_id": 777,
                            "photo": [
                                "https://cdn1.ozone.ru/s3/multimedia/image.jpg"
                            ],
                            "errors": [],
                        }
                    ]
                }

            def product_pictures_import(
                self, _offer_id: str, _images: list[str]
            ) -> dict:
                return {"result": {"task_id": 456}}

            def attributes_update(self, _items: list[dict]) -> dict:
                self.rich_update_calls += 1
                return {"result": {}}

            def product_info_attributes(self, _offer_ids: list[str]) -> dict:
                return {"items": [{"offer_id": "QSM102401", "attributes": []}]}

        rich = {
            "version": 0.3,
            "content": [
                {
                    "widgetName": "raShowcase",
                    "blocks": [
                        {
                            "img": {
                                "src": "https://cdn.example.com/ai-2.jpg",
                                "srcMobile": "https://cdn.example.com/ai-2.jpg",
                            }
                        }
                    ],
                }
            ],
        }
        assembly = AssemblyResult(
            payload={
                "items": [
                    {
                        "offer_id": "QSM102401",
                        "name": "Ароматическая подвеска для шкафа и комнаты, декоративный ароматизатор для домашнего пространства",
                        "attributes": [
                            {
                                "id": 999,
                                "complex_id": 0,
                                "values": [{"value": json.dumps(rich)}],
                            }
                        ],
                        "images": [
                            f"https://cdn.example.com/ai-{index}.jpg"
                            for index in range(1, 9)
                        ],
                    }
                ]
            },
            offer_ids=["QSM102401"],
            validation_errors=[],
            warnings=[],
            dimension_source="test",
            required_attribute_ids=[],
        )
        client = _Client()
        with tempfile.TemporaryDirectory() as temporary:
            log = UploadLog(Path(temporary) / "upload.json")
            first = run_upload_workflow(
                store=_store(),
                assembly=assembly,
                upload_log=log,
                execute=True,
                client=client,
                poll_interval_seconds=0,
                image_hosting_max_polls=1,
                verification_max_polls=1,
            )
            first_status = first["status"]
            first_rich_content_deferred = first["rich_content_deferred"]
            second = run_upload_workflow(
                store=_store(),
                assembly=assembly,
                upload_log=log,
                execute=True,
                client=client,
                poll_interval_seconds=0,
                image_hosting_max_polls=1,
                verification_max_polls=1,
            )

        self.assertEqual(first_status, "VERIFIED")
        self.assertTrue(first_rich_content_deferred)
        self.assertEqual(second["status"], "VERIFIED")
        self.assertFalse(second["rich_content_deferred"])
        self.assertEqual(second["post_publish_warnings"], [])
        self.assertEqual(client.product_import_calls, 1)
        self.assertEqual(client.rich_update_calls, 1)

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

            self.assertEqual(changed["status"], "SKIPPED_UNCERTAIN_SUBMISSION")
            self.assertEqual(changed["duplicate_offer_ids"], ["QSM102401"])
            self.assertEqual(client.product_import_calls, 1)

    def test_changed_payload_upserts_when_previous_task_id_is_known(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload.json"
            client = _ImportClient(response={"result": {"task_id": 24680}})

            first = run_upload_workflow(
                store=_store(),
                assembly=_assembly(),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
                max_polls=1,
                poll_interval_seconds=0,
            )
            changed = run_upload_workflow(
                store=_store(),
                assembly=_assembly(marker="images-now-on-oss"),
                upload_log=UploadLog(path),
                execute=True,
                client=client,
                max_polls=1,
                poll_interval_seconds=0,
            )

            self.assertEqual(first["status"], "POLL_TIMEOUT")
            self.assertEqual(changed["status"], "POLL_TIMEOUT")
            self.assertEqual(client.product_import_calls, 2)


if __name__ == "__main__":
    unittest.main()
