#!/usr/bin/env python3
"""Replicate the supplied OGL workbook's batch pricing formulas for CSV rows."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")
MONEY = D("0.01")

INPUT_FIELDS = [
    "product_name",
    "sale_price_cny",
    "purchase_cost_cny",
    "domestic_misc_cny",
    "return_rate_pct",
    "withdrawal_rate_pct",
    "ads_rate_pct",
    "last_mile_rate_pct",
    "commission_rate_pct",
    "weight_g",
    "length_cm",
    "width_cm",
    "height_cm",
    "exchange_rate_rub_per_cny",
    "target_margin_pct",
    "supplier_url",
    "product_facts",
]

OUTPUT_FIELDS = [
    "product_value_rub",
    "ogl_channel",
    "volumetric_weight_g",
    "billable_weight_g",
    "first_mile_cny",
    "commission_cny",
    "return_loss_cny",
    "withdrawal_fee_cny",
    "ads_cny",
    "last_mile_cny",
    "total_cost_cny",
    "profit_cny",
    "sales_margin_pct",
    "cost_roi_pct",
    "break_even_price_cny",
    "suggested_price_cny",
    "target_met",
    "warnings",
]

DEFAULTS = {
    "domestic_misc_cny": D("0"),
    "return_rate_pct": D("3"),
    "withdrawal_rate_pct": D("1.2"),
    "ads_rate_pct": D("5"),
    "last_mile_rate_pct": D("2"),
    "commission_rate_pct": D("10"),
    "length_cm": D("0"),
    "width_cm": D("0"),
    "height_cm": D("0"),
    "exchange_rate_rub_per_cny": D("12.6"),
    "target_margin_pct": D("30"),
}

TARIFFS = {
    "Extra Small": (D("2.90"), D("0.0247"), False),
    "Budget": (D("22.25"), D("0.0164"), False),
    "Small": (D("15.48"), D("0.0242"), False),
    "Big": (D("34.82"), D("0.0164"), True),
    "Premium Small": (D("21.28"), D("0.0242"), False),
    "Premium Big": (D("59.97"), D("0.0222"), True),
}


def number(row: dict[str, str], field: str, line: int) -> Decimal:
    raw = (row.get(field) or "").strip()
    if not raw and field in DEFAULTS:
        return DEFAULTS[field]
    if not raw:
        raise ValueError(f"line {line}: missing {field}")
    try:
        value = D(raw)
    except InvalidOperation as exc:
        raise ValueError(f"line {line}: invalid number for {field}: {raw!r}") from exc
    if value < ZERO:
        raise ValueError(f"line {line}: {field} cannot be negative")
    return value


def rate(row: dict[str, str], field: str, line: int) -> Decimal:
    raw = (row.get(field) or "").strip()
    if raw.endswith("%"):
        row = dict(row)
        row[field] = raw[:-1]
        value = number(row, field, line) / HUNDRED
    else:
        value = number(row, field, line)
        if value >= ONE:
            value /= HUNDRED
    if value > ONE:
        raise ValueError(f"line {line}: {field} cannot exceed 100%")
    return value


def q(value: Decimal) -> str:
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP))


def qp(value: Decimal) -> str:
    return str((value * HUNDRED).quantize(MONEY, rounding=ROUND_HALF_UP))


def band(value_rub: Decimal) -> int:
    if value_rub <= D("1500"):
        return 1
    if value_rub <= D("7000"):
        return 2
    return 3


def calculate(row: dict[str, str], line: int = 2) -> dict[str, str]:
    sale = number(row, "sale_price_cny", line)
    purchase = number(row, "purchase_cost_cny", line)
    misc = number(row, "domestic_misc_cny", line)
    weight = number(row, "weight_g", line)
    length = number(row, "length_cm", line)
    width = number(row, "width_cm", line)
    height = number(row, "height_cm", line)
    exchange = number(row, "exchange_rate_rub_per_cny", line)

    commission_rate = rate(row, "commission_rate_pct", line)
    return_rate = rate(row, "return_rate_pct", line)
    withdrawal_rate = rate(row, "withdrawal_rate_pct", line)
    ads_rate = rate(row, "ads_rate_pct", line)
    last_mile_rate = rate(row, "last_mile_rate_pct", line)
    target_margin = rate(row, "target_margin_pct", line)

    if sale <= ZERO:
        raise ValueError(f"line {line}: sale_price_cny must be greater than zero")
    if weight <= ZERO:
        raise ValueError(f"line {line}: weight_g must be greater than zero")
    if weight > D("35000"):
        raise ValueError(f"line {line}: weight exceeds the OGL 35kg limit")
    if exchange <= ZERO:
        raise ValueError(f"line {line}: exchange_rate_rub_per_cny must be greater than zero")

    value_rub = sale * exchange
    if weight <= D("500") and value_rub <= D("1500"):
        channel = "Extra Small"
    elif value_rub <= D("1500"):
        channel = "Budget"
    elif weight <= D("2000"):
        channel = "Small" if value_rub <= D("7000") else "Premium Small"
    elif value_rub <= D("7000"):
        channel = "Big"
    else:
        channel = "Premium Small" if weight <= D("5000") else "Premium Big"

    fixed, per_gram, use_volumetric = TARIFFS[channel]
    dimensions_complete = length > ZERO and width > ZERO and height > ZERO
    volumetric = length * width * height / D("12000") * D("1000") if dimensions_complete else weight
    billable = max(weight, volumetric) if use_volumetric else weight
    first_mile = fixed + per_gram * billable

    commission = sale * commission_rate
    return_loss = sale * return_rate
    withdrawal = sale * withdrawal_rate
    ads = sale * ads_rate
    last_mile = min(max(sale * exchange * last_mile_rate, D("15")), D("200")) / exchange
    total_cost = purchase + misc + first_mile + commission + return_loss + withdrawal + ads + last_mile
    profit = sale - total_cost
    sales_margin = profit / sale
    cost_roi = profit / total_cost if total_cost else ZERO

    all_rates = commission_rate + return_rate + withdrawal_rate + ads_rate + last_mile_rate
    break_even_denominator = ONE - all_rates
    suggested_denominator = ONE - all_rates - target_margin
    if break_even_denominator <= ZERO:
        raise ValueError(f"line {line}: variable rates must total less than 100%")
    if suggested_denominator <= ZERO:
        raise ValueError(f"line {line}: rates plus target margin must total less than 100%")
    fixed_cost = purchase + misc + first_mile
    break_even = fixed_cost / break_even_denominator
    suggested = fixed_cost / suggested_denominator

    warnings: list[str] = []
    if use_volumetric and not dimensions_complete:
        warnings.append("volumetric OGL band: dimensions missing; actual weight used provisionally")
    if sales_margin < target_margin:
        warnings.append("sales margin is below target")
    if band(value_rub) != band(suggested * exchange):
        warnings.append("suggested price crosses an OGL value band; rerun with suggested price")
    if length > D("200") or width > D("80") or height > D("80"):
        warnings.append("package may exceed the 200x80x80cm OGL limit")
    if dimensions_complete and length * width * height > D("500000"):
        warnings.append("package may exceed the 500L OGL volume limit")

    output = dict(row)
    output.update(
        {
            "product_value_rub": q(value_rub),
            "ogl_channel": channel,
            "volumetric_weight_g": q(volumetric),
            "billable_weight_g": q(billable),
            "first_mile_cny": q(first_mile),
            "commission_cny": q(commission),
            "return_loss_cny": q(return_loss),
            "withdrawal_fee_cny": q(withdrawal),
            "ads_cny": q(ads),
            "last_mile_cny": q(last_mile),
            "total_cost_cny": q(total_cost),
            "profit_cny": q(profit),
            "sales_margin_pct": qp(sales_margin),
            "cost_roi_pct": qp(cost_roi),
            "break_even_price_cny": q(break_even),
            "suggested_price_cny": q(suggested),
            "target_met": "yes" if sales_margin >= target_margin else "no",
            "warnings": "; ".join(warnings),
        }
    )
    return output


def run(input_path: Path, output_path: Path) -> int:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output paths must be different")
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in INPUT_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("missing CSV columns: " + ", ".join(missing))
        rows = [calculate(row, line) for line, row in enumerate(reader, start=2)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INPUT_FIELDS + OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        count = run(args.input, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Calculated {count} row(s): {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
