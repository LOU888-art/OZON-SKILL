# OZON-SKILL

1688 → OZON 商品流水线，包含一个本地网页版 ERP、五阶段门禁、JSON 产物审计和默认 dry-run 的上传状态机。

## 网页版 ERP

ERP 为每个 1688 商品创建独立目录：`work/<offer_id>/`，并严格按顺序生成 `01_raw.json`、`02_mapped.json`、`03_final.json`、`04_payload.json` 和 `05_upload_log.json`。前一阶段未通过门禁时，服务端会拒绝运行下一阶段。

启动：

```bash
python3 -m venv --system-site-packages /workspace/.venv
/workspace/.venv/bin/python -m pip install -e . --no-build-isolation
/workspace/.venv/bin/ozon-erp --host 127.0.0.1 --port 8080
```

浏览器打开 `http://127.0.0.1:8080`。AlphaShop 密钥必须在进程环境中配置。OZON Seller API 店铺可在网页的“店铺授权”中添加：ERP 会先通过只读类目树接口验证 Client-Id 与 Api-Key，再将凭证保存到权限为 `0600` 的服务端文件；Api-Key 不会通过网页或列表 API 回显。

顶部“定价工具”按 `最新定价工具表.xlsx` 的人民币口径计算 OGL/邮政分档、计泡重量、头程、佣金、退货、提现、广告、尾程、保本价和目标利润建议价。店铺经营参数保存后，订单利润看板与第四阶段 SKU 自动上架价会复用同一套规则；采购成本和上架售价分开保存，不再把“采购价 × 汇率”误当作销售定价。

顶部“任务中心”集中显示全部商品任务、运行进度、失败门禁和系统配置告警。所有者与管理员还可查看操作审计；审计只记录操作者、接口、时间和结果，不保存请求正文、店铺密钥或 AI 密钥。任务中心不会自动重放第五阶段的真实上架请求。

顶部“通知”按当前登录成员保存门禁失败的已读状态。通知和任务中心的“修复 / 重试”只会打开对应阶段的修复表单，不会自动调用 AlphaShop、AI 或 Ozon；任何可能产生费用的操作仍需人工提交，第五阶段还必须再次勾选真实上架确认。

常用环境变量：

- AlphaShop 商品详情、类目映射和图片翻译：`ALPHASHOP_ACCESS_KEY`、`ALPHASHOP_SECRET_KEY`。
- OZON 店铺凭证与币种：优先通过网页“店铺授权”管理，支持多店铺；原有 `OZON_CLIENT_ID`、`OZON_API_KEY`、`OZON_CURRENCY` 仅保留为兼容回退。
- 安全开关：缺省视为 `DRY_RUN=true`。只有进程环境明确设置 `DRY_RUN=false`，且操作者在网页再次勾选执行，才允许调用 `/v3/product/import`。

每个商品页面都可以运行七项全链路一致性审查。ERP 不提供 mock 执行入口，生产工作区中的数据只能来自官方 API；测试 fixture 只供自动测试使用。

## 1688 商品采集

生产流水线通过 AlphaShop 的 `alphashop.openclaw.offer.detail.query/1.0` 实时获取商品详情，并输出固定的 `01_raw.json`。输入既可以是纯数字商品 ID，也可以是包含 `/offer/<id>.html` 的 1688 商品链接。

运行真实请求前，通过环境变量提供凭证：

- `ALPHASHOP_ACCESS_KEY`
- `ALPHASHOP_SECRET_KEY`

客户端使用 HS256 JWT，声明为 `iss`、`exp=now+1800`、`nbf=now-5`。凭证只从上述环境变量读取，日志和产物不会保存完整 token 或密钥。

安装并调用：

```bash
collect-1688 'https://detail.1688.com/offer/945957565364.html' \
  --output work/945957565364/01_raw.json
```

生产 ERP 没有 mock 入口。缺少凭证时会直接停止；AlphaShop 返回欠费或余额不足时不会重试或生成伪数据。

标准化规则：

- 价格输出为 JSON 数值，币种固定解释为 CNY。
- 图片保留 API 返回的原始 URL；有尺寸时保留宽高，没有时为 `null`。
- API 未返回的字段输出 `null`，不会生成业务默认值。
- `category_id` 优先使用 `thirdCategoryId`，缺失时使用 `categoryId`。
- 详情图优先读取结构化字段，并从官方响应的详情 HTML 中提取图片地址。

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 1688 → OZON 类目与属性映射

这一阶段先调用 AlphaShop 类目映射获得候选 category/type，再要求该候选真实存在于本次 OZON 类目树。属性定义和字典值仍由 OZON 实时接口获取。所有 `attribute_id` 和 `dictionary_value_id` 都来自当前调用结果，不允许生成或手写。

网页版 ERP 的真实元数据请求使用当前流水线选择的已验证 OZON 店铺授权。独立命令行仍可使用 `OZON_CLIENT_ID` 与 `OZON_API_KEY`。

```bash
PYTHONPATH=src python3 -m ozon_skill.category_mapping.cli \
  sample.json \
  --output ozon-listing-pre.json
```

没有凭证时使用固定 API mock：

```bash
PYTHONPATH=src python3 -m ozon_skill.category_mapping.cli \
  sample.json \
  --mock-dir tests/fixtures/ozon \
  --output ozon-listing-pre.json
```

默认自动选择阈值为 `0.70`。低于阈值的类目、缺失的必填属性、无法唯一命中的字典值都会进入 `needs_confirmation`，不会填充占位值。输出中的 `metadata_provenance` 记录了每次元数据请求及响应哈希。

## 俄语内容与 Rich-Контент

内容阶段合并 `ozon-listing-pre.json` 中的属性，不调用上架接口：

```bash
PYTHONPATH=src python3 -m ozon_skill.content_generation.cli \
  ozon-listing-pre.json \
  --source sample.json \
  --output ozon-listing-final.json
```

ERP 会把主图、详情图和去重后的 SKU 图逐张提交给 AlphaShop 图片翻译 PRO，源语言为 `auto`、目标语言为 `ru`。任一图片未取得合法 HTTPS 翻译 URL，阶段 3 门禁都会失败，不会回退到未翻译图片继续上传。

Rich-Контент构建器强制使用 `{content, version: 0.3}` 外层和 `raShowcase`，并要求每个 block 同时包含 `imgLink`、`img`、`text`、`title`。裸数组、`raImage` 顶层组件和禁用图床会被拒绝。

## Payload 组装与上传追踪

上传命令默认是 dry-run；只有将 `DRY_RUN` 显式设置为 `false` 时才会创建 OZON 客户端并尝试写请求：

```bash
PYTHONPATH=src python3 -m ozon_skill.upload.cli \
  ozon-listing-final.json \
  --source sample.json \
  --config tests/fixtures/upload-config.json \
  --payload-output upload-dry-run.json \
  --upload-log upload_log.json
```

网页版 ERP 的上传配置使用已授权店铺的 `store_id`、后台币种、CNY→目标币种汇率和缺少结构化尺寸时采用的显式估算值。店铺币种在授权时确认，只接受 `RUB` 或 `CNY`。

执行模式需要同时满足：

- `ozon-listing-final.json` 状态为 `READY_FOR_PAYLOAD_ASSEMBLY`；
- 没有缺失的必填属性、图片、价格或尺寸；
- `DRY_RUN=false`；
- `OZON_CURRENCY` 已配置且与卖家后台币种一致；
- 每个店铺都配置 `OZON_<CREDENTIAL_PREFIX>_CLIENT_ID` 和 `OZON_<CREDENTIAL_PREFIX>_API_KEY`。

执行状态机依次完成 import、立即持久化 `task_id`、轮询 import/info、可选属性补写、Archived 解归档、info/list 回查及必填属性回查。日志中已有相同店铺和 `offer_id` 的 `task_id` 时拒绝重复 import。模块不包含库存、仓库或激活接口。
