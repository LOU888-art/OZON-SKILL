from __future__ import annotations

import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.service import PipelineError, PipelineService
from ozon_skill.erp.supplier_monitor import (
    SupplierMonitorError,
    SupplierMonitorRegistry,
    SupplierScheduleRegistry,
    compare_snapshots,
    snapshot_from_raw,
)


def _raw(*, price: str = "12.80", stock: int | None = 10) -> dict:
    return {
        "title_zh": "香薰石牌",
        "supplier_info": {"name": "测试供应商"},
        "sku_list": [
            {
                "sku_id": "sku-1",
                "purchase_price_cny": price,
                "stock": stock,
                "specification_text_zh": "白茶",
            }
        ],
    }


class SupplierMonitorTests(unittest.TestCase):
    def test_detects_price_change_and_out_of_stock(self) -> None:
        previous = snapshot_from_raw(_raw(price="12.80", stock=10))
        current = snapshot_from_raw(_raw(price="15.00", stock=0))
        changes = compare_snapshots(previous, current)
        kinds = {item["type"] for item in changes}

        self.assertIn("PRICE_CHANGED", kinds)
        self.assertIn("OUT_OF_STOCK", kinds)

    def test_unknown_stock_is_not_reported_as_out_of_stock(self) -> None:
        snapshot = snapshot_from_raw(_raw(stock=None))
        self.assertEqual(snapshot["stock_status"], "UNKNOWN")

    def test_registry_preserves_baseline_and_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".supplier-monitor.json"
            registry = SupplierMonitorRegistry(path)
            baseline = registry.record("123", _raw())
            changed = registry.record("123", _raw(price="13.50"))
            status = registry.status()
            mode = os.stat(path).st_mode & 0o777

        self.assertEqual(baseline["check_status"], "BASELINE")
        self.assertEqual(changed["check_status"], "CHANGED")
        self.assertEqual(status["summary"]["changed"], 1)
        self.assertEqual(status["events"][0]["type"], "PRICE_CHANGED")
        self.assertEqual(mode, 0o600)

    def test_refresh_requires_confirmation_and_limits_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with self.assertRaisesRegex(PipelineError, "确认"):
                service.refresh_supplier_monitor({"pipeline_ids": ["123"]})
            with self.assertRaisesRegex(PipelineError, "20"):
                service.refresh_supplier_monitor(
                    {
                        "pipeline_ids": [str(1000 + index) for index in range(21)],
                        "confirm_external_requests": True,
                    }
                )

    def test_restart_interrupts_job_without_replaying_paid_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = PipelineService(root)
            with patch.dict(
                os.environ,
                {"ALPHASHOP_ACCESS_KEY": "test-key", "ALPHASHOP_SECRET_KEY": "test-secret"},
            ), patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_supplier_monitor(
                    {"pipeline_ids": ["123"], "confirm_external_requests": True},
                    "QSM",
                )

            restarted = PipelineService(root)
            result = restarted.supplier_monitor_job_status(job["job_id"])

        self.assertEqual(result["status"], "INTERRUPTED")
        self.assertEqual(result["items"][0]["status"], "INTERRUPTED")
        self.assertIn("避免重复", result["message"])

    def test_background_check_does_not_overwrite_stage_one_artifact(self) -> None:
        class Client:
            def offer_detail_exchange(self, pipeline_id: str):
                return type("Exchange", (), {"payload": {}})()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "123" / "01_raw.json"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(json.dumps(_raw()), encoding="utf-8")
            original = source_path.read_bytes()
            service = PipelineService(root)
            with patch(
                "ozon_skill.erp.service.AlphaShopClient.from_environment",
                return_value=Client(),
            ), patch("ozon_skill.erp.service.threading.Thread.start"):
                job = service.start_supplier_monitor(
                    {"pipeline_ids": ["123"], "confirm_external_requests": True}
                )
            with patch(
                "ozon_skill.erp.service.AlphaShopClient.from_environment",
                return_value=Client(),
            ), patch(
                "ozon_skill.erp.service.normalize_alphashop_offer",
                return_value=_raw(price="15.00"),
            ):
                service._run_supplier_monitor(job["job_id"])
            result = service.supplier_monitor_job_status(job["job_id"])
            unchanged_source = source_path.read_bytes()

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(unchanged_source, original)

    def test_schedule_requires_recurring_cost_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = SupplierScheduleRegistry(Path(temporary) / ".schedule.json")
            with self.assertRaisesRegex(SupplierMonitorError, "周期性"):
                registry.save(
                    {"enabled": True, "pipeline_ids": ["123"], "interval_hours": 24}
                )

    def test_schedule_is_claimed_once_and_advances_next_run(self) -> None:
        start = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            registry = SupplierScheduleRegistry(Path(temporary) / ".schedule.json")
            saved = registry.save(
                {
                    "enabled": True,
                    "pipeline_ids": ["123"],
                    "interval_hours": 6,
                    "confirm_recurring_charges": True,
                },
                now=start,
            )
            claimed = registry.claim_due(now=start + timedelta(hours=6))
            second = registry.claim_due(now=start + timedelta(hours=6))

        self.assertEqual(saved["next_run_at"], "2026-10-23T06:00:00Z")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["next_run_at"], "2026-10-23T12:00:00Z")
        self.assertIsNone(second)

    def test_restart_defers_overdue_schedule_without_claiming_it(self) -> None:
        start = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            registry = SupplierScheduleRegistry(Path(temporary) / ".schedule.json")
            registry.save(
                {
                    "enabled": True,
                    "pipeline_ids": ["123"],
                    "interval_hours": 6,
                    "confirm_recurring_charges": True,
                },
                now=start,
            )
            deferred = registry.defer_overdue(now=start + timedelta(hours=9))
            claimed = registry.claim_due(now=start + timedelta(hours=9))

        self.assertEqual(deferred["last_status"], "DEFERRED_AFTER_RESTART")
        self.assertEqual(deferred["next_run_at"], "2026-10-23T15:00:00Z")
        self.assertIsNone(claimed)

    def test_service_saves_admin_schedule_only_for_collected_products(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "123" / "01_raw.json"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(json.dumps(_raw()), encoding="utf-8")
            service = PipelineService(root)
            with patch.dict(
                os.environ,
                {"ALPHASHOP_ACCESS_KEY": "test-key", "ALPHASHOP_SECRET_KEY": "test-secret"},
            ), patch("ozon_skill.erp.service.threading.Thread.start"):
                schedule = service.save_supplier_schedule(
                    {
                        "enabled": True,
                        "pipeline_ids": ["123"],
                        "interval_hours": 24,
                        "confirm_recurring_charges": True,
                    },
                    "QSM",
                )
            mode = os.stat(root / ".supplier-monitor-schedule.json").st_mode & 0o777

        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["updated_by"], "QSM")
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
