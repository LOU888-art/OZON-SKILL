from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ozon_skill.erp.offer_ids import OfferIdRegistry, OfferIdRegistryError
from ozon_skill.upload.assembler import assemble_import_payload
from ozon_skill.upload.config import DimensionDefaults, StoreConfig


class OfferIdRegistryTests(unittest.TestCase):
    def test_assigns_qsm_daily_sequence_and_reuses_it_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".ozon-offer-ids.json"
            clock = lambda: datetime(2026, 10, 23, 8, tzinfo=timezone.utc)
            first = OfferIdRegistry(path, clock=clock)

            self.assertEqual(
                first.assign(["store|product|sku-a", "store|product|sku-b"]),
                ["QSM102301", "QSM102302"],
            )
            restarted = OfferIdRegistry(path, clock=clock)
            self.assertEqual(
                restarted.assign(["store|product|sku-a", "store|other|sku-a"]),
                ["QSM102301", "QSM102303"],
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_skips_reserved_ids_and_uses_the_next_calendar_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".ozon-offer-ids.json"
            first = OfferIdRegistry(
                path,
                clock=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc),
            )
            self.assertEqual(
                first.assign(["one"], reserved_ids=["QSM102301", "QSM102302"]),
                ["QSM102303"],
            )
            tomorrow = OfferIdRegistry(
                path,
                clock=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            self.assertEqual(tomorrow.assign(["two"]), ["QSM102401"])

    def test_rejects_duplicate_allocation_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = OfferIdRegistry(Path(temporary) / "ids.json")
            with self.assertRaisesRegex(OfferIdRegistryError, "分配键重复"):
                registry.assign(["same", "same"])

    def test_payload_uses_assigned_offer_ids_in_sku_order(self) -> None:
        source = {
            "sku_list": [
                {"sku_id": "a", "purchase_price_cny": "10", "sale_price_cny": "20"},
                {"sku_id": "b", "purchase_price_cny": "12", "sale_price_cny": "24"},
            ]
        }
        final = {
            "source_offer_id": "1688-id",
            "title_ru": "Товар",
            "ozon_type_id": 1,
            "ozon_category_id": 2,
            "listing_readiness": "READY_FOR_PAYLOAD_ASSEMBLY",
            "rich_content_json": {"content": []},
            "ozon_attributes": [],
        }
        store = StoreConfig(
            store_id="store",
            credential_prefix="STORE",
            cny_to_rub_rate=Decimal("1"),
            dimension_defaults=DimensionDefaults(100, 100, 100, 100),
            currency_code="CNY",
        )

        result = assemble_import_payload(
            final,
            source,
            store,
            assigned_offer_ids=["QSM102301", "QSM102302"],
        )

        self.assertEqual(
            [item["offer_id"] for item in result.payload["items"]],
            ["QSM102301", "QSM102302"],
        )


if __name__ == "__main__":
    unittest.main()
