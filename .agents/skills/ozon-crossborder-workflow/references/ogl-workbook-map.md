# OGL workbook mapping

This reference is derived from the user-supplied `OGL定价工具表.xlsx`. The workbook is the business source of truth; rates and formulas may change in later versions.

## Workbook sheets

- `使用说明` — scope, assumptions, and operating notes
- `定价核算` — single-product calculation
- `批量核算` — rows for multiple products
- `类目佣金` — OZON category names and FBO/FBS/RFBS rates
- `费率参数` — exchange rate, target margin, default fee references, and OGL tariff table

## Single-product input mapping

| Cell | Meaning |
|---|---|
| C4 | Product name |
| C5 | RFBS commission rate |
| C7 | Sale price, CNY |
| C8 | Purchase cost, CNY |
| C9 | Domestic miscellaneous cost, CNY |
| C10 | Return/cancellation loss rate |
| C11 | Withdrawal/settlement fee rate |
| C12 | Advertising rate |
| C13 | Last-mile rate |
| C15 | Packed actual weight, g |
| C16:C18 | Length, width, and height, cm |

Key calculated outputs are C19:C36 and the validation message in B38.

## Batch input mapping

| Column | Meaning |
|---|---|
| A | Product name |
| B | Sale price, CNY |
| C | Purchase cost, CNY |
| D | Domestic miscellaneous cost, CNY |
| E | Return/cancellation rate |
| F | Withdrawal/settlement rate |
| G | Advertising rate |
| H | Last-mile rate |
| I | RFBS commission rate |
| J | Actual packed weight, g |
| K:M | Length, width, and height, cm |

Calculated outputs are N:AD:

- billable weight and OGL channel
- first-mile logistics
- itemized costs and total cost
- unit profit, sales margin, and cost ROI
- break-even and suggested prices

## Current global parameters

- Exchange rate: `12.6 RUB/CNY`
- Target sales margin: `30%`
- Reference RFBS commission: `10%`
- Return/cancellation reference: `3%`
- Withdrawal reference: `1.2%`
- Advertising reference: `5%`
- Last-mile reference: `2%`, subject to a `15–200 RUB` per-item cap before conversion to CNY

These are defaults, not permanent marketplace facts. Prefer the current workbook and user-supplied per-product values.

## OGL tariffs in the supplied workbook

| Channel | Weight/value rule | Fixed CNY | CNY/g | Volumetric |
|---|---|---:|---:|---|
| Extra Small | 1–500g and value ≤1500 RUB | 2.90 | 0.0247 | No |
| Budget | 501–35000g and value ≤1500 RUB | 22.25 | 0.0164 | No |
| Small | 1–2000g and value 1501–7000 RUB | 15.48 | 0.0242 | No |
| Big | 2001–35000g and value 1501–7000 RUB | 34.82 | 0.0164 | Yes |
| Premium Small | 1–5000g and value 7001–250000 RUB | 21.28 | 0.0242 | No |
| Premium Big | 5001–35000g and value 7001–250000 RUB | 59.97 | 0.0222 | Yes |

For Big and Premium Big:

```text
volumetric_weight_g = length_cm × width_cm × height_cm ÷ 12000 × 1000
billable_weight_g = max(actual_weight_g, volumetric_weight_g)
```

The workbook documents an actual-weight maximum of 35kg, package dimensions up to 200×80×80cm, and volume up to 500L.

## Pricing formulas

```text
first_mile = fixed_fee + per_gram_fee × billable_weight
commission = sale_price × commission_rate
return_loss = sale_price × return_rate
withdrawal = sale_price × withdrawal_rate
advertising = sale_price × advertising_rate
last_mile = min(max(sale_price × exchange_rate × last_mile_rate, 15), 200) ÷ exchange_rate
total_cost = purchase + miscellaneous + first_mile + commission + return_loss + withdrawal + advertising + last_mile
profit = sale_price - total_cost
sales_margin = profit ÷ sale_price
cost_roi = profit ÷ total_cost
break_even = (purchase + miscellaneous + first_mile) ÷ (1 - all variable rates)
suggested_price = (purchase + miscellaneous + first_mile) ÷ (1 - all variable rates - target margin)
```

The reverse-price formulas use the last-mile percentage rather than its RUB cap. Preserve that behavior when reproducing the workbook, and flag material cap-related differences for review.
