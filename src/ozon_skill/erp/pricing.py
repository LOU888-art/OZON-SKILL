"""Ozon rFBS pricing rules mirrored from the supplied pricing workbook."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping


class PricingError(RuntimeError):
    """A user-facing pricing input error."""


OGL_RATES = {
    "Extra Small": (Decimal("2.90"), Decimal("0.0247")),
    "Budget": (Decimal("22.25"), Decimal("0.0164")),
    "Small": (Decimal("15.48"), Decimal("0.0242")),
    "Big": (Decimal("34.82"), Decimal("0.0164")),
    "Premium Small": (Decimal("21.28"), Decimal("0.0242")),
    "Premium Big": (Decimal("59.97"), Decimal("0.0222")),
}


def _number(value: Any, name: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PricingError(f"{name}必须是数字") from exc
    if not result.is_finite() or result < minimum:
        raise PricingError(f"{name}不能小于 {minimum}")
    return result


def _rate(value: Any, name: str) -> Decimal:
    result = _number(value, name)
    if result > 100:
        raise PricingError(f"{name}不能超过 100%")
    return result / 100


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _channel(
    sale_price_cny: Decimal,
    rate: Decimal,
    weight_g: Decimal,
    logistics_mode: str,
    postal_transport: str,
) -> str:
    goods_value_rub = sale_price_cny * rate
    if logistics_mode == "OGL":
        if weight_g > 35000:
            raise PricingError("OGL 实重不能超过 35kg")
        if weight_g <= 500 and goods_value_rub <= 1500:
            return "Extra Small"
        if goods_value_rub <= 1500:
            return "Budget"
        if weight_g <= 2000:
            return "Small" if goods_value_rub <= 7000 else "Premium Small"
        if goods_value_rub <= 7000:
            return "Big"
        return "Premium Small" if weight_g <= 5000 else "Premium Big"
    if logistics_mode != "邮政":
        raise PricingError("物流方式必须是 OGL 或邮政")
    if postal_transport not in {"空运", "陆运"}:
        raise PricingError("邮政运输必须是空运或陆运")
    if weight_g <= 500 and goods_value_rub <= 1500:
        return f"WH{postal_transport}(特小件)"
    if weight_g <= 5000:
        return "e邮宝(空运)" if postal_transport == "空运" else "e邮宝特惠(陆运)"
    limit = Decimal("30000") if postal_transport == "空运" else Decimal("20000")
    if weight_g > limit:
        raise PricingError(f"邮政{postal_transport}限重 {int(limit / 1000)}kg")
    return "e特快(空运)" if postal_transport == "空运" else "e包裹特惠(陆运)"


def _freight(
    channel: str,
    weight_g: Decimal,
    length_cm: Decimal,
    width_cm: Decimal,
    height_cm: Decimal,
) -> tuple[Decimal, Decimal]:
    charge_weight = weight_g
    if channel in {"Big", "Premium Big"} and all(value > 0 for value in (length_cm, width_cm, height_cm)):
        volumetric = length_cm * width_cm * height_cm / Decimal("12000") * 1000
        charge_weight = max(weight_g, volumetric)
    if channel in OGL_RATES:
        fixed, per_gram = OGL_RATES[channel]
        return fixed + per_gram * charge_weight, charge_weight
    kilograms = charge_weight / 1000
    if channel == "WH空运(特小件)":
        return Decimal("1.9") + Decimal("67") * kilograms, charge_weight
    if channel == "WH陆运(特小件)":
        return Decimal("1.9") + Decimal("26") * kilograms, charge_weight
    if channel == "e邮宝(空运)":
        return Decimal("15") + Decimal("65") * kilograms, charge_weight
    if channel == "e邮宝特惠(陆运)":
        return Decimal("15") + Decimal("34.75") * kilograms, charge_weight
    if channel == "e特快(空运)":
        steps = math.ceil(float(max(charge_weight - 50, Decimal("0")) / 50))
        return Decimal("102") + Decimal("2.55") * steps, charge_weight
    if channel == "e包裹特惠(陆运)":
        steps = math.ceil(float(max(charge_weight - 500, Decimal("0")) / 500))
        return Decimal("52.5") + Decimal("15") * steps, charge_weight
    raise PricingError("未找到适用的物流费率")


def calculate_pricing(value: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate unit economics using the workbook's exact public inputs."""

    sale = _number(value.get("sale_price_cny"), "Ozon 售价", minimum=Decimal("0.01"))
    purchase = _number(value.get("purchase_cost_cny"), "采购成本")
    misc = _number(value.get("domestic_misc_cny", 0), "国内杂费")
    exchange = _number(value.get("cny_to_rub_rate", 12.6), "人民币兑卢布汇率", minimum=Decimal("0.01"))
    weight = _number(value.get("weight_g"), "实重", minimum=Decimal("0.01"))
    length = _number(value.get("length_cm", 0), "长度")
    width = _number(value.get("width_cm", 0), "宽度")
    height = _number(value.get("height_cm", 0), "高度")
    commission = _rate(value.get("commission_percent", 10), "类目佣金率")
    returns = _rate(value.get("return_loss_percent", 3), "退货/取消损失率")
    withdrawal = _rate(value.get("withdrawal_percent", 1.2), "提现/结汇手续费率")
    advertising = _rate(value.get("advertising_percent", 5), "广告费率")
    last_mile = _rate(value.get("last_mile_percent", 2), "尾程配送费率")
    target = _rate(value.get("target_margin_percent", 30), "目标销售利润率")
    variable_rate = commission + returns + withdrawal + advertising + last_mile
    if variable_rate >= 1:
        raise PricingError("佣金及各项比例费率合计必须小于 100%")
    if variable_rate + target >= 1:
        raise PricingError("费率与目标利润率合计必须小于 100%")
    mode = str(value.get("logistics_mode") or "OGL").strip()
    transport = str(value.get("postal_transport") or "空运").strip()
    channel = _channel(sale, exchange, weight, mode, transport)
    freight, charge_weight = _freight(channel, weight, length, width, height)
    platform_fee = sale * commission
    return_loss = sale * returns
    withdrawal_fee = sale * withdrawal
    advertising_fee = sale * advertising
    last_mile_fee = min(max(sale * exchange * last_mile, Decimal("15")), Decimal("200")) / exchange
    total = purchase + misc + freight + platform_fee + return_loss + withdrawal_fee + advertising_fee + last_mile_fee
    profit = sale - total
    break_even = (purchase + misc + freight) / (1 - variable_rate)
    suggested = (purchase + misc + freight) / (1 - variable_rate - target)
    warnings: list[str] = []
    if mode == "OGL" and channel in {"Big", "Premium Big"} and not all(item > 0 for item in (length, width, height)):
        warnings.append("OGL 计抛档缺少长宽高，当前暂按实重计费")
    if profit / sale < target:
        warnings.append("销售毛利率低于目标，请提价或降本")
    suggested_channel = _channel(suggested, exchange, weight, mode, transport)
    if suggested_channel != channel:
        warnings.append(f"建议售价会使物流从 {channel} 跨档到 {suggested_channel}，请按建议价再次试算")
    return {
        "schema_version": "1.0",
        "currency": "CNY",
        "channel": channel,
        "charge_weight_g": str(charge_weight.quantize(Decimal("0.01"))),
        "goods_value_rub": _money(sale * exchange),
        "fees": {
            "purchase_cost": _money(purchase),
            "domestic_misc": _money(misc),
            "first_mile_logistics": _money(freight),
            "platform_commission": _money(platform_fee),
            "return_cancel_loss": _money(return_loss),
            "withdrawal_exchange": _money(withdrawal_fee),
            "advertising": _money(advertising_fee),
            "last_mile_delivery": _money(last_mile_fee),
        },
        "total_cost_cny": _money(total),
        "unit_profit_cny": _money(profit),
        "sales_margin_percent": str((profit / sale * 100).quantize(Decimal("0.01"))),
        "cost_margin_percent": str((profit / total * 100).quantize(Decimal("0.01"))) if total else None,
        "break_even_price_cny": _money(break_even),
        "suggested_price_cny": _money(suggested),
        "warnings": warnings,
    }


def calculate_target_pricing(value: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a target-margin price while allowing OGL value tiers to stabilize."""

    purchase = _number(value.get("purchase_cost_cny"), "采购成本")
    misc = _number(value.get("domestic_misc_cny", 0), "国内杂费")
    seed = max(purchase + misc, Decimal("0.01"))
    previous_channel = ""
    result: dict[str, Any] | None = None
    for _ in range(8):
        result = calculate_pricing({**value, "sale_price_cny": seed})
        suggested = Decimal(result["suggested_price_cny"])
        channel = str(result["channel"])
        if abs(suggested - seed) < Decimal("0.01") and channel == previous_channel:
            break
        seed = suggested
        previous_channel = channel
    result = calculate_pricing({**value, "sale_price_cny": seed})
    result["suggested_price_cny"] = _money(seed)
    return result
