# 在 OpenAI Codex 中使用

## 项目级安装

把本仓库切换到 `codex-ozon-workflow` 分支并在 Codex 中打开。Skill 位于：

```text
.agents/skills/ozon-crossborder-workflow/SKILL.md
```

项目级方式不会修改 Codex 的运行时缓存，也不会被运行时升级覆盖。

## 全局安装（可选）

如需在所有项目中使用，可把整个 Skill 文件夹复制到：

```text
C:\Users\HP\.codex\skills\ozon-crossborder-workflow
```

不要修改 `.cache\codex-runtimes` 中的内置 Skill；更新 Codex 时该目录可能被覆盖。

## 推荐输入

将以下文件拖入 Codex 会话或放到当前工作区：

- 最新版 `OGL定价工具表.xlsx`
- 待分析的商品 CSV/XLSX
- 供应商报价、尺寸重量、材质和图片
- 可选的 OZON 销售、广告和库存导出表

## 最简调用

```text
使用 $ozon-crossborder-workflow，读取 OGL定价工具表.xlsx 和商品表.xlsx，
先做利润核算与风险筛选，再为通过的商品生成俄语商品卡草稿。
所有发布、改价和广告操作必须先让我确认。
```

只做定价：

```text
使用 $ozon-crossborder-workflow，严格按照 OGL定价工具表.xlsx 计算这些商品的
总成本、利润、毛利率、保本价和建议售价，并列出缺少的数据。
```

做周度复盘：

```text
使用 $ozon-crossborder-workflow 分析本周的订单、广告、退货和库存文件，
按 SKU 给出 keep、optimize 或 pause 建议。
```

## 本机已有 Skill 如何参与

Codex 当前会话只有在明确显示某个 Skill 可用时才能调用它。典型协作方式：

- `spreadsheets`：读写和分析 Excel/CSV
- `excel-live-control`：让本机 Excel 重新计算公式
- `documents`：生成 Word 报告
- `pdf`：读取供应商资料或输出 PDF
- `presentations`：生成选品汇报
- `playwright-cli`：在授权范围内检查公开商品页

CodeArts、OpenClaw、Zcode 缓存目录中的 Skill 不会自动成为 Codex Skill。
