from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ozon_skill.erp.market_research import MarketResearchError, MarketResearchRegistry
from ozon_skill.erp.service import PipelineError, PipelineService


RULES = {
    "date_from": "2026-01-03",
    "date_to": "2026-10-22",
    "include_keywords": "lamp, лампа",
    "exclude_keywords": "used",
    "brand_mode": "unbranded",
    "price_min_rub": 300,
    "price_max_rub": 2000,
    "orders_min": 10,
    "sellers_max": 8,
    "max_candidates": 50,
}


def candidate(*, product_id: str = "123", title: str = "LED lamp") -> dict:
    return {
        "ozon_product_id": product_id,
        "title": title,
        "product_url": f"https://www.ozon.ru/product/{product_id}/",
        "image_url": f"https://cdn1.ozone.ru/s3/{product_id}.jpg",
        "category": "Lighting",
        "brand": "Без бренда",
        "seller": "Seller",
        "price_rub": "899",
        "orders": "42",
        "revenue_rub": "37758",
        "sellers": "3",
        "rating": "4.8",
        "reviews": "19",
        "growth_percent": "18.2",
        "daily_sales": "1.4",
        "ad_share_percent": "12.5",
        "card_views": "6400",
        "conversion_rate_percent": "2.4",
        "return_rate_percent": "7.5",
        "days_on_sale": "95",
    }


class MarketResearchTests(unittest.TestCase):
    def test_rules_allow_arbitrary_valid_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            rules = registry.save_rules(RULES, "operator")
            self.assertEqual(rules["date_from"], "2026-01-03")
            self.assertEqual(rules["date_to"], "2026-10-22")
            self.assertEqual(os.stat(registry.path).st_mode & 0o777, 0o600)

    def test_rules_reject_reversed_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            with self.assertRaisesRegex(MarketResearchError, "开始日期"):
                registry.save_rules({**RULES, "date_from": "2026-10-23", "date_to": "2026-01-01"}, "operator")

    def test_rules_reject_invalid_ranges_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            with self.assertRaisesRegex(MarketResearchError, "最低价格"):
                registry.save_rules({**RULES, "price_min_rub": 2000, "price_max_rub": 300}, "operator")
            with self.assertRaisesRegex(MarketResearchError, "评分"):
                registry.save_rules({**RULES, "rating_min": 6}, "operator")
            with self.assertRaisesRegex(MarketResearchError, "候选池上限"):
                registry.save_rules({**RULES, "max_candidates": "many"}, "operator")

    def test_plugin_ingest_filters_deduplicates_and_preserves_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            registry.save_rules(RULES, "operator")
            first = registry.ingest([candidate(), candidate(product_id="bad", title="used LED lamp")], "plugin")
            self.assertEqual(first["accepted"], 1)
            self.assertEqual(first["rejected"], 1)
            registry.update("123", {"decision": "SHORTLISTED"}, "operator")
            second = registry.ingest([candidate()], "plugin")
            self.assertEqual(second["accepted"], 1)
            self.assertEqual(second["updated"], 1)
            status = registry.status()
            self.assertEqual(status["summary"]["total"], 1)
            self.assertEqual(status["summary"]["shortlisted"], 1)
            self.assertEqual(status["candidates"][0]["decision"], "SHORTLISTED")
            self.assertGreater(float(status["candidates"][0]["opportunity_score"]), 0)
            self.assertEqual(status["candidates"][0]["score_version"], "observable-v1")

    def test_advanced_metrics_are_strict_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            registry.save_rules(
                {
                    **RULES,
                    "daily_sales_min": 1,
                    "card_views_min": 5000,
                    "conversion_rate_min": 2,
                    "ad_share_max_percent": 15,
                    "return_rate_max_percent": 10,
                    "days_on_sale_max": 120,
                    "opportunity_score_min": 40,
                },
                "operator",
            )
            accepted = registry.ingest([candidate()], "plugin")
            self.assertEqual(accepted["accepted"], 1)
            weak = candidate(product_id="weak")
            weak["conversion_rate_percent"] = "0.5"
            rejected = registry.ingest([weak], "plugin")
            self.assertEqual(rejected["accepted"], 0)
            self.assertEqual(rejected["rejected"], 1)

    def test_required_advanced_metric_is_never_estimated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            registry.save_rules({**RULES, "card_views_min": 1}, "operator")
            missing = candidate()
            missing.pop("card_views")
            result = registry.ingest([missing], "plugin")
            self.assertEqual(result["accepted"], 0)
            self.assertEqual(result["rejected"], 1)

    def test_named_profiles_keep_independent_candidate_pools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            registry.save_rules({**RULES, "profile_id": "blue", "profile_name": "蓝海"}, "operator")
            first = registry.ingest([candidate()], "plugin")
            self.assertEqual(first["candidate_ids"], ["blue--123"])
            registry.save_rules({**RULES, "profile_id": "growth", "profile_name": "增长"}, "operator")
            second = registry.ingest([candidate()], "plugin")
            self.assertEqual(second["candidate_ids"], ["growth--123"])
            status = registry.status()
            self.assertEqual(len(status["profiles"]), 2)
            self.assertEqual(status["active_profile_id"], "growth")
            self.assertEqual(len(status["candidates"]), 2)

    def test_metric_history_only_adds_changed_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            registry.save_rules(RULES, "operator")
            registry.ingest([candidate()], "plugin")
            registry.ingest([candidate()], "plugin")
            changed = candidate()
            changed["orders"] = "50"
            registry.ingest([changed], "plugin")
            row = registry.get("123")
            self.assertEqual(len(row["metric_history"]), 2)
            self.assertEqual(row["metric_history"][-1]["orders"], "50")

    def test_ingest_requires_saved_rules_and_ozon_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = MarketResearchRegistry(Path(temporary) / "market.json")
            with self.assertRaisesRegex(MarketResearchError, "先在 ERP"):
                registry.ingest([candidate()], "plugin")
            registry.save_rules(RULES, "operator")
            bad = candidate()
            bad["product_url"] = "https://example.com/product/123"
            result = registry.ingest([bad], "plugin")
            self.assertEqual(result["accepted"], 0)
            self.assertEqual(result["rejected"], 1)

    def test_service_binds_confirmed_1688_source_to_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            service.save_market_research_rules(RULES, "operator")
            service.ingest_market_candidates({"items": [candidate()]}, "plugin")
            with self.assertRaisesRegex(PipelineError, "确认允许"):
                service.source_market_candidate("123", {"offer_id": "628680616884"}, "operator")
            with patch.object(service, "run_stage_1", return_value={"pipeline_id": "628680616884", "status": "PASS"}):
                with patch.object(service.stores, "list_public", return_value=[{"store_id": "store-1"}]), patch.object(service, "read_artifact", return_value={"sku_list": []}):
                    result = service.source_market_candidate(
                        "123",
                        {"offer_id": "628680616884", "store_id": "store-1", "confirm_external_request": True},
                        "operator",
                    )
            self.assertEqual(result["candidate"]["decision"], "SOURCED")
            self.assertEqual(result["candidate"]["pipeline_id"], "628680616884")
            self.assertEqual(result["candidate"]["economics"]["status"], "NEEDS_INPUT")

    def test_missing_candidate_never_calls_paid_source_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = PipelineService(Path(temporary))
            with patch.object(service, "run_stage_1") as run_stage_1:
                with self.assertRaisesRegex(PipelineError, "候选商品不存在"):
                    service.source_market_candidate(
                        "missing",
                        {"offer_id": "628680616884", "confirm_external_request": True},
                        "operator",
                    )
            run_stage_1.assert_not_called()


if __name__ == "__main__":
    unittest.main()
