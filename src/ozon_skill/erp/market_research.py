"""Ozon market-candidate rules and persistent browser-plugin intake."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


class MarketResearchError(RuntimeError):
    """A user-facing market research error."""


DECISIONS = {"NEW", "SHORTLISTED", "APPROVED", "REJECTED", "SOURCED"}
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value: Any, name: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise MarketResearchError(f"{name}必须是有效日期") from exc


def _number(value: Any, name: str, *, minimum: Decimal | None = None) -> str | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketResearchError(f"{name}必须是数字") from exc
    if not result.is_finite() or (minimum is not None and result < minimum):
        raise MarketResearchError(f"{name}不能小于 {minimum}")
    return str(result)


def _https_url(value: Any, name: str, *, ozon_only: bool = False) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise MarketResearchError(f"{name}必须是 HTTPS 地址")
    if ozon_only and host != "ozon.ru" and not host.endswith(".ozon.ru"):
        raise MarketResearchError(f"{name}必须指向 Ozon")
    return text[:2000]


def normalize_rules(value: Mapping[str, Any]) -> dict[str, Any]:
    start = _date(value.get("date_from"), "开始日期")
    end = _date(value.get("date_to"), "结束日期")
    if start > end:
        raise MarketResearchError("开始日期不能晚于结束日期")
    include = str(value.get("include_keywords") or "").strip()[:500]
    exclude = str(value.get("exclude_keywords") or "").strip()[:500]
    try:
        max_candidates = int(value.get("max_candidates") or 100)
    except (TypeError, ValueError) as exc:
        raise MarketResearchError("候选池上限必须是整数") from exc
    rules = {
        "date_from": start,
        "date_to": end,
        "category": str(value.get("category") or "").strip()[:200],
        "include_keywords": include,
        "exclude_keywords": exclude,
        "brand_mode": str(value.get("brand_mode") or "all") if str(value.get("brand_mode") or "all") in {"all", "branded", "unbranded"} else "all",
        "price_min_rub": _number(value.get("price_min_rub"), "最低价格", minimum=Decimal("0")),
        "price_max_rub": _number(value.get("price_max_rub"), "最高价格", minimum=Decimal("0")),
        "orders_min": _number(value.get("orders_min"), "最低订购量", minimum=Decimal("0")),
        "revenue_min_rub": _number(value.get("revenue_min_rub"), "最低订购金额", minimum=Decimal("0")),
        "sellers_max": _number(value.get("sellers_max"), "最多卖家数", minimum=Decimal("0")),
        "rating_min": _number(value.get("rating_min"), "最低评分", minimum=Decimal("0")),
        "reviews_min": _number(value.get("reviews_min"), "最低评论数", minimum=Decimal("0")),
        "growth_min_percent": _number(value.get("growth_min_percent"), "最低增长率"),
        "max_candidates": max(1, min(500, max_candidates)),
    }
    if rules["price_min_rub"] is not None and rules["price_max_rub"] is not None:
        if Decimal(rules["price_min_rub"]) > Decimal(rules["price_max_rub"]):
            raise MarketResearchError("最低价格不能高于最高价格")
    if rules["rating_min"] is not None and Decimal(rules["rating_min"]) > Decimal("5"):
        raise MarketResearchError("最低评分不能高于 5")
    return rules


def _matches(candidate: Mapping[str, Any], rules: Mapping[str, Any]) -> tuple[bool, list[str]]:
    title = str(candidate.get("title") or "").casefold()
    include = [part.casefold() for part in re.split(r"[,，;；\n]+", str(rules.get("include_keywords") or "")) if part.strip()]
    exclude = [part.casefold() for part in re.split(r"[,，;；\n]+", str(rules.get("exclude_keywords") or "")) if part.strip()]
    reasons: list[str] = []
    if include and not any(word.strip() in title for word in include):
        return False, ["未命中包含关键词"]
    if any(word.strip() in title for word in exclude):
        return False, ["命中排除关键词"]
    if include:
        reasons.append("命中关键词")
    category = str(rules.get("category") or "").strip().casefold()
    if category and category not in str(candidate.get("category") or "").casefold():
        return False, ["类目不匹配"]
    brand_mode = rules.get("brand_mode")
    has_brand = bool(str(candidate.get("brand") or "").strip()) and str(candidate.get("brand") or "").casefold() not in {"без бренда", "no brand", "无品牌"}
    if brand_mode == "unbranded" and has_brand:
        return False, ["不是无品牌商品"]
    if brand_mode == "branded" and not has_brand:
        return False, ["缺少品牌"]
    checks = (
        ("price_rub", "price_min_rub", lambda actual, limit: actual >= limit, "价格达到下限"),
        ("price_rub", "price_max_rub", lambda actual, limit: actual <= limit, "价格未超过上限"),
        ("orders", "orders_min", lambda actual, limit: actual >= limit, "订购量达到下限"),
        ("revenue_rub", "revenue_min_rub", lambda actual, limit: actual >= limit, "订购金额达到下限"),
        ("sellers", "sellers_max", lambda actual, limit: actual <= limit, "卖家数未超过上限"),
        ("rating", "rating_min", lambda actual, limit: actual >= limit, "评分达到下限"),
        ("reviews", "reviews_min", lambda actual, limit: actual >= limit, "评论数达到下限"),
        ("growth_percent", "growth_min_percent", lambda actual, limit: actual >= limit, "增长率达到下限"),
    )
    for field, rule, compare, reason in checks:
        if rules.get(rule) is None:
            continue
        try:
            actual = Decimal(str(candidate.get(field)))
            limit = Decimal(str(rules[rule]))
        except (InvalidOperation, TypeError):
            return False, [f"缺少筛选指标 {field}"]
        if not compare(actual, limit):
            return False, [f"未满足 {rule}"]
        reasons.append(reason)
    return True, reasons or ["符合当前筛选规则"]


class MarketResearchRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "rules": None, "candidates": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarketResearchError("选品候选池无法读取") from exc
        if not isinstance(value, dict) or not isinstance(value.get("candidates"), dict):
            raise MarketResearchError("选品候选池格式无效")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent, text=True)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def status(self) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            rows = sorted(document["candidates"].values(), key=lambda row: str(row.get("updated_at") or ""), reverse=True)
            summary = {"total": len(rows), "new": 0, "shortlisted": 0, "approved": 0, "rejected": 0, "sourced": 0}
            for row in rows:
                key = str(row.get("decision") or "NEW").lower()
                if key in summary:
                    summary[key] += 1
            return {"schema_version": "1.0", "rules": document.get("rules"), "summary": summary, "candidates": rows, "generated_at": _now()}

    def save_rules(self, value: Mapping[str, Any], actor: str) -> dict[str, Any]:
        rules = normalize_rules(value)
        with self._lock:
            document = self._read()
            document["rules"] = {**rules, "updated_at": _now(), "updated_by": actor or "system"}
            self._write(document)
            return dict(document["rules"])

    def ingest(self, items: Any, actor: str) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise MarketResearchError("插件没有提交商品数据")
        if len(items) > 500:
            raise MarketResearchError("单次最多同步 500 个 Ozon 商品")
        with self._lock:
            document = self._read()
            rules = document.get("rules")
            if not isinstance(rules, Mapping):
                raise MarketResearchError("请先在 ERP 保存选品规则")
            accepted = updated = rejected = 0
            accepted_ids: list[str] = []
            for item in items:
                if not isinstance(item, Mapping):
                    rejected += 1
                    continue
                try:
                    url = _https_url(item.get("product_url"), "Ozon 商品链接", ozon_only=True)
                    title = str(item.get("title") or "").strip()[:500]
                    if not title:
                        raise MarketResearchError("商品名称不能为空")
                    product_id = str(item.get("ozon_product_id") or "").strip()
                    if not _PRODUCT_ID.fullmatch(product_id):
                        product_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                    normalized = {
                        "candidate_id": product_id,
                        "ozon_product_id": str(item.get("ozon_product_id") or "").strip()[:100],
                        "title": title,
                        "product_url": url,
                        "image_url": _https_url(item.get("image_url"), "商品图片") if item.get("image_url") else "",
                        "category": str(item.get("category") or "").strip()[:300],
                        "brand": str(item.get("brand") or "").strip()[:200],
                        "seller": str(item.get("seller") or "").strip()[:200],
                        "price_rub": _number(item.get("price_rub"), "价格", minimum=Decimal("0")),
                        "orders": _number(item.get("orders"), "订购量", minimum=Decimal("0")),
                        "revenue_rub": _number(item.get("revenue_rub"), "订购金额", minimum=Decimal("0")),
                        "sellers": _number(item.get("sellers"), "卖家数", minimum=Decimal("0")),
                        "rating": _number(item.get("rating"), "评分", minimum=Decimal("0")),
                        "reviews": _number(item.get("reviews"), "评论数", minimum=Decimal("0")),
                        "growth_percent": _number(item.get("growth_percent"), "增长率"),
                    }
                    matches, reasons = _matches(normalized, rules)
                    if not matches:
                        rejected += 1
                        continue
                    previous = document["candidates"].get(product_id) or {}
                    normalized.update({
                        "decision": previous.get("decision") or "NEW",
                        "source_offer_id": previous.get("source_offer_id"),
                        "pipeline_id": previous.get("pipeline_id"),
                        "match_reasons": reasons,
                        "rule_period": {"date_from": rules["date_from"], "date_to": rules["date_to"]},
                        "first_seen_at": previous.get("first_seen_at") or _now(),
                        "updated_at": _now(),
                        "updated_by": actor or "plugin",
                    })
                    document["candidates"][product_id] = normalized
                    accepted += 1
                    updated += 1 if previous else 0
                    accepted_ids.append(product_id)
                    if accepted >= int(rules.get("max_candidates") or 100):
                        break
                except MarketResearchError:
                    rejected += 1
            self._write(document)
            return {"accepted": accepted, "updated": updated, "rejected": rejected, "candidate_ids": accepted_ids}

    def update(self, candidate_id: str, value: Mapping[str, Any], actor: str) -> dict[str, Any]:
        decision = str(value.get("decision") or "").strip().upper()
        if decision not in DECISIONS:
            raise MarketResearchError("候选品状态无效")
        with self._lock:
            document = self._read()
            row = document["candidates"].get(candidate_id)
            if not isinstance(row, dict):
                raise MarketResearchError("候选商品不存在")
            row.update({"decision": decision, "updated_at": _now(), "updated_by": actor or "system"})
            self._write(document)
            return dict(row)

    def get(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._read()["candidates"].get(candidate_id)
            if not isinstance(row, dict):
                raise MarketResearchError("候选商品不存在")
            return dict(row)

    def bind_source(self, candidate_id: str, offer_id: str, pipeline_id: str, actor: str) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            row = document["candidates"].get(candidate_id)
            if not isinstance(row, dict):
                raise MarketResearchError("候选商品不存在")
            row.update({"decision": "SOURCED", "source_offer_id": offer_id, "pipeline_id": pipeline_id, "updated_at": _now(), "updated_by": actor or "system"})
            self._write(document)
            return dict(row)

    def delete(self, candidate_id: str) -> None:
        with self._lock:
            document = self._read()
            if document["candidates"].pop(candidate_id, None) is None:
                raise MarketResearchError("候选商品不存在")
            self._write(document)
