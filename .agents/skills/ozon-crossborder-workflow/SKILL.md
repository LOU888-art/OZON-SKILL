---
name: ozon-crossborder-workflow
description: Run an evidence-based OZON cross-border e-commerce workflow using the user's OGL pricing workbook for product intake, selection screening, deterministic pricing and profit calculation, Russian listing drafts, compliance review, and operating reports. Use when the user asks about OZON selection, rFBS/OGL pricing, profitability, listings, or sales analysis.
---

# OZON cross-border workflow for Codex

Produce decision-ready files while keeping source facts, assumptions, calculations, drafts, and approved outputs separate. Use the user's current OGL workbook as the primary pricing authority whenever it is supplied.

## Capability routing

Use only Skills advertised as available in the current Codex session. A file found in another product's cache does not make that Skill callable in Codex.

- Use `spreadsheets` to inspect, copy, populate, calculate, and export workbook data.
- Use `excel-live-control` when native Excel recalculation or interaction with an already-open workbook is required.
- Use `documents`, `pdf`, or `presentations` only when the user asks for those deliverables.
- Use `playwright-cli` only for authorized public-page checks or form preparation. Never bypass CAPTCHA, login controls, rate limits, or marketplace policy.
- `data-analysis`, `i18n-integration`, and `ozon-product-selection` found under CodeArts/OpenClaw are not Codex dependencies unless the current Codex session explicitly exposes them. Apply the equivalent steps in this Skill when they are absent.

Read `references/ogl-workbook-map.md` before changing or calculating the OGL workbook.

## Safety and approval boundaries

- Never request passwords, cookies, one-time codes, or API keys in chat.
- Never invent demand, sales, certifications, materials, dimensions, brand rights, or product claims.
- Do not publish products, change live prices or inventory, buy ads, message customers, or delete marketplace data without explicit confirmation immediately before the action.
- Treat marketplace fees, exchange rates, logistics prices, policies, and search results as time-sensitive. Record the source and date.
- Do not overwrite the source workbook or supplier files. Work on copies under the run directory.

## Create a run directory

Use `runs/YYYYMMDD-topic/` with:

- `source/` — unmodified user inputs
- `working/` — normalized tables and workbook copies
- `output/` — pricing, selection, listing, and report files
- `decision-log.md` — assumptions, evidence dates, exclusions, and approvals

## Stage 1: intake and normalization

Accept CSV/XLSX files, supplier quotations, product links, images, PDFs, or pasted rows. Normalize candidate products to `templates/product-intake.csv`.

Minimum fields for a provisional price decision:

- product name
- sale price in CNY
- purchase cost in CNY
- RFBS commission rate
- packed weight in grams

Ask only for missing information that blocks useful progress. Preserve unknowns as blank fields and label affected outputs provisional.

## Stage 2: evidence-based product screening

Score every candidate from 0 to 5 on:

1. evidence of demand
2. competitive intensity
3. contribution margin and price headroom
4. logistics suitability, including weight, volume, fragility, and return risk
5. differentiation potential
6. compliance and intellectual-property risk

Record the evidence and date for every score. Use `advance`, `hold`, or `reject`. A high score cannot override a compliance blocker or negative profit.

If browser checks are requested and `playwright-cli` is available, inspect only authorized or public pages. Do not claim search results represent full marketplace demand.

## Stage 3: OGL pricing and profit

When the OGL workbook is available:

1. Copy it into `working/`; never edit the original.
2. Use the workbook's `类目佣金` sheet to find the precise RFBS commission when possible.
3. For one product, populate the yellow inputs on `定价核算`.
4. For several products, populate columns A:M on `批量核算` using the mapping in the reference.
5. Recalculate with a spreadsheet capability that supports formulas. Do not trust stale cached formula values after changing inputs.
6. Read the calculated OGL channel, logistics cost, total cost, profit, margin, break-even price, suggested price, and validation warnings.

If workbook recalculation is unavailable, run the deterministic fallback:

```text
python .agents/skills/ozon-crossborder-workflow/scripts/ogl_pricing.py \
  --input <normalized.csv> --output <pricing.csv>
```

The fallback follows the supplied workbook's documented formulas. Label it as a replicated calculation and retain the workbook as the business source of truth.

If the suggested price crosses an OGL value band, rerun using the suggested price. Repeat until the logistics band and suggested price stabilize or report that manual review is required.

## Stage 4: decision gate

Default thresholds come from the workbook or user settings. Do not silently replace them.

Mark a product `advance` only when:

- profit is positive
- sales margin meets the target
- logistics inputs are complete
- no unresolved compliance blocker exists
- the suggested price has been rerun if it crossed an OGL band

Otherwise use `hold` or `reject` and state the exact reason.

## Stage 5: Russian listing draft

For products marked `advance`, generate:

- natural Russian title
- category suggestion
- five factual benefit bullets
- product description
- Russian search terms without keyword stuffing
- required attributes, with unknown fields clearly marked
- main and supporting image brief
- Chinese back-translation for review

Preserve brands, model numbers, measurements, and regulated terms. Do not add certifications, superlatives, medical claims, guarantees, or specifications absent from evidence.

## Stage 6: compliance review

Create `output/compliance-review.csv` with:

- prohibited or restricted category risk
- trademark, copyright, and counterfeit risk
- required declarations or certificates
- labeling and package-language requirements
- battery, liquid, cosmetic, food, children's-product, and medical-device concerns
- weight and dimensions consistency
- unsupported claims

Use `blocked`, `needs-evidence`, or `clear-for-manual-review`. Only a human can approve publication.

## Stage 7: deliverables

Always create:

- normalized product table
- pricing and profit table
- selection decision table
- decision log
- listing drafts for advanced products
- unresolved-risk list

When requested, use the available document Skills to produce an XLSX analysis workbook, PDF report, DOCX report, or presentation. Keep Markdown and CSV versions as portable fallbacks.

## Stage 8: weekly operating review

When sales data is supplied, analyze traffic, conversion, revenue, contribution profit, advertising, returns, stock, and replenishment by SKU. Separate observed facts from hypotheses. Recommend `keep`, `optimize`, or `pause`, with one measurable next action and review date.

## Final response

Report:

- files and date ranges used
- candidates advanced, held, and rejected
- pricing assumptions and sensitivity risks
- OGL band and recalculation status
- compliance blockers
- files created
- decisions requiring human approval

Never describe drafts as published, cached formulas as recalculated, or estimated marketplace data as verified.
