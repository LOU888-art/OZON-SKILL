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

生产登录由 ERP 应用自身管理，不再使用浏览器 Basic Auth 弹窗。用户可以在 `/login` 使用邮箱或含国家区号的手机号自行注册；密码使用 scrypt 加盐哈希保存，会话 Cookie 设置 `HttpOnly`、`Secure` 和 `SameSite=Lax`，退出登录后服务端立即撤销会话。自行注册的账号固定为“受限试用”，只能使用定价工具以及查看、验证和删除本人绑定的 Ozon 店铺，其他页面隐藏且对应后端接口返回 403。可通过 `ERP_SELF_REGISTRATION=false` 暂停新注册。

如需把指定邮箱或手机号绑定为现有工作区主账号，请在服务器运行 `bash deploy/aliyun/set-owner-login.sh`。脚本会通过隐藏输入读取新密码，把登录标识绑定到 `.team.json` 中唯一的所有者身份，并撤销该所有者的全部旧会话；不会在源码或终端输出密码。

阿里云生产环境从 `/opt/ozon-erp/venv` 运行已安装的软件包，不能只在源码目录执行 `git pull` 后重启。更新生产版本必须执行：

```bash
cd /root/ozon-erp-source
git pull --ff-only origin deploy/aliyun
bash deploy/aliyun/update.sh
```

更新器会重新安装运行包、重启服务、检查健康状态，并核验关键新版静态页面已经进入实际运行目录；不会修改 `/var/lib/ozon-erp/work` 中的商品数据或 `/etc/ozon-erp` 中的密钥配置。

顶部“定价工具”按 `OZON最新定价表.xlsx` 的人民币口径计算 OGL/邮政分档、计泡重量、头程、佣金、退货、提现、广告、尾程、保本价和目标利润建议价。定价页内置表格中去重后的 10,788 条 Ozon 商品类型，可用中文或俄文搜索；选中后按售价折算货值自动使用 `≤1500₽` / `1500.01–5000₽` / `>5000₽` 三档 RFBS 佣金，保本价和建议价跨档时会重新取佣金。未选类目时仍可手动填写佣金，保持旧数据兼容。店铺经营参数保存后，订单利润看板与第四阶段 SKU 自动上架价会复用同一套物流与成本规则；采购成本和上架售价分开保存，不再把“采购价 × 汇率”误当作销售定价。

顶部“Ozon 选品”与 `browser-extension/northstar-ozon-research` 浏览器插件组成市场选品闭环。运营可保存任意开始/结束日期、类目、关键词、品牌、价格、订购量、销售额、卖家数、评分、评论数和增长率规则。插件读取当前已登录 Ozon Seller“分析 → Ozon 上的商品”结果表，可自动滚动加载，再交由 ERP 服务端复核、去重并同步到候选池。插件不保存 ERP/Ozon 密码或 Cookie，必须通过已登录 ERP 标签页同源调用接口。

候选品可按独立选品方案管理，支持风险/利润筛选、2–4 款横向对比、批量通过或淘汰。高风险通过必须二次确认，且只有已审核通过的商品才能进入 1688 货源预审。每个候选品可一次采集 1–5 个真实货源，比较采购价、运费、库存、SKU 与 SKU 图片完整度，并按所选 Ozon 店铺配置计算真实利润；资料不完整或利润不为正的货源不能创建任务。运营确认最佳货源后，系统直接把预审缓存提升为第一阶段产物，不会重复调用 AlphaShop。当前版本已预留“以图搜 1688”入口，但在未配置可验证的图搜 API 前不会伪造搜索结果，可先手动粘贴真实 1688 货源链接。插件安装见 [browser-extension/northstar-ozon-research/README.md](browser-extension/northstar-ozon-research/README.md)。

顶部“任务中心”集中显示全部商品任务、运行进度、失败门禁和系统配置告警。所有者与管理员还可查看操作审计；审计只记录操作者、接口、时间和结果，不保存请求正文、店铺密钥或 AI 密钥。任务中心不会自动重放第五阶段的真实上架请求。

商品工作台、商品中心、Ozon 选品候选池、任务中心和图片资产库支持单选、全选当前筛选结果和批量删除。商品任务删除只清理 ERP 本地采集资料与阶段产物，不调用 Ozon 删除商品接口；运行中的任务会被拦截。图片资产只允许删除无商品引用的 OSS 托管图片，曾进入成功上架记录的图片永久保护。

顶部“通知”按当前登录成员保存门禁失败的已读状态。通知和任务中心的“修复 / 重试”只会打开对应阶段的修复表单，不会自动调用 AlphaShop、AI 或 Ozon；任何可能产生费用的操作仍需人工提交，第五阶段还必须再次勾选真实上架确认。

第五阶段在请求 Ozon 前会先持久化提交意图。若连接中断、进程退出或 Ozon 响应缺少 `task_id`，系统会将结果标记为“需要人工核对”，并禁止同一店铺、同一 `offer_id` 自动重复提交；请先在 Ozon 后台确认商品是否已创建，再决定后续处理。取得 `task_id` 后再次运行会继续回查原任务，不会重新创建商品。

第四阶段会在组装 Payload 前进行完整预检：前端一次性列出全部缺失采购价、无效尺寸、汇率和必填属性；后端会汇总所有 SKU 的采购价与自动定价错误，并将结构化预检结果写入 `04_payload.json`。修复时无需再逐条运行、逐条发现。

任务中心的“外部通知”允许所有者或管理员配置企业微信机器人和 SMTP 邮件。只有总开关与对应渠道同时启用后，新产生的门禁失败才会发送；相同商品、阶段和错误摘要只通知一次，服务器重启不会补发旧错误。企业微信只接受官方 `qyapi.weixin.qq.com` HTTPS Webhook。Webhook 与 SMTP 密码以权限 `0600` 保存在服务器且永不通过 API 回显，发送历史也不保存通知正文。保存后可使用“发送测试”验证渠道，该操作会立即发送一条真实测试消息。

顶部“图片存储”可配置阿里云 OSS 与公开 CDN 地址。配置通过后，第三阶段会下载已经生成或翻译完成的图片，校验真实 PNG/JPEG/WebP 内容，按 SHA-256 去重上传到自有 Bucket，并把商品图和 Rich Content 中的临时外链替换为稳定 HTTPS 地址。保存配置时会上传并公开读取一张 1×1 验证图；建议使用仅有目标 Bucket `PutObject` 权限的 RAM 子账号，如需从图片资产库清理孤儿对象，再增加 `DeleteObject` 权限。ERP 不设置对象级 ACL，图片继承 Bucket ACL 或 Bucket Policy；请将 Bucket 设为公共读，或配置能够公开回源的 CDN。

“图片资产”页面会聚合 1688 原图、翻译图、AI 生成图、OSS 大小与商品/SKU 引用。只有当前无任何商品引用且从未进入成功上架记录的 OSS 对象才会显示删除按钮；已上架图片永久保护。

“商品中心”会把全部流水线整理成经营视图，统一展示草稿、处理中、门禁失败、待上架、已上架和需要更新的商品，并汇总 SKU 数量、Ozon 类目/类型、售价区间、店铺和五阶段进度。该页面只读取已有产物，不会调用 AI 或 Ozon，也不会产生外部费用。

商品中心支持勾选最多 50 个商品批量运行到第 2、3 或 4 阶段。系统逐件串行执行并跳过已经 PASS 的阶段，单件失败不会阻断其他商品；第三阶段可能调用翻译或 AI 生图，因此创建任务前必须明确确认外部费用。任务进度会持久化，服务器重启后为避免重复扣费不会自动续跑。第五阶段永远不进入批处理，真实上架仍必须逐个商品人工确认。

商品中心还支持最多 50 个商品批量编辑目标店铺、CNY→RUB 汇率、默认尺寸重量和 VAT。只有显式勾选的字段才会覆盖；类目属性、SKU 采购价、图片方案和任何店铺密钥均不会跨商品复制。每次实际变更都会分别保存初始版本和新版本，可从商品行的“版本”入口查看或回滚。编辑或回滚后第四、第五阶段会自动标记为需要更新，但系统不会自动重组 Payload、调用 AI 或提交 Ozon。

商品中心的“供应商检测”可对选中的最多 20 个商品建立独立 1688 快照，并在后台检测逐 SKU 采购价变化、库存、断货、补货、SKU 新增/移除、供应商变化和商品失效。创建检测任务前必须明确确认 AlphaShop 接口调用；服务器重启后不会自动续跑未完成任务。检测不会覆盖 `01_raw.json`，也不会自动修改定价、库存或 Ozon 商品。若已配置外部通知，检测到变化时会同时发送企业微信或邮件提醒。

所有者和管理员还可以在供应商检测窗口配置周期计划，支持每 6/12/24/72/168 小时运行，最多监控 20 个商品。启用时必须明确确认持续的 AlphaShop 接口费用，并且只有凭证可用、商品已经完成第一阶段采集时才允许保存。服务器停机期间错过的计划会在重启时顺延到下一个周期，不会集中补跑；暂停或删除计划不会删除已有监控历史。

商品工作区的“批量导入 ID / 链接”可一次提交最多 100 条 1688 商品。系统会先提取 ID、去重和检查套餐商品额度，在用户明确确认后才后台逐条调用 AlphaShop。进度与每条错误会持久化；服务器重启后不会自动重放未完成的付费请求。

常用环境变量：

- AlphaShop 商品详情、类目映射和图片翻译：`ALPHASHOP_ACCESS_KEY`、`ALPHASHOP_SECRET_KEY`。
- OZON 店铺凭证与币种：优先通过网页“店铺授权”管理，支持多店铺；原有 `OZON_CLIENT_ID`、`OZON_API_KEY`、`OZON_CURRENCY` 仅保留为兼容回退。
- 安全开关：缺省视为 `DRY_RUN=true`。只有进程环境明确设置 `DRY_RUN=false`，且操作者在网页再次勾选执行，才允许调用 `/v3/product/import`。
- 性能调优：Ozon 类目元数据默认在进程内缓存 900 秒；AI 图片和 OSS 图片固化默认各使用 4 个并发任务。可通过 `OZON_METADATA_CACHE_TTL_SECONDS`、`ERP_AI_IMAGE_WORKERS`、`ERP_IMAGE_IO_WORKERS` 调整。AI 并发强制限制为最多 6，图片 I/O 最多 8，避免误配置导致接口限流。

每个商品页面都可以运行七项全链路一致性审查。ERP 不提供 mock 执行入口，生产工作区中的数据只能来自官方 API；测试 fixture 只供自动测试使用。

## 1688 商品采集

生产流水线通过 AlphaShop 的 `alphashop.openclaw.offer.detail.query/1.0` 实时获取商品详情，并输出固定的 `01_raw.json`。输入既可以是纯数字商品 ID，也可以是包含 `/offer/<id>.html` 的 1688 商品链接。

也可以从 ERP 左侧下载 `Northstar 1688 商品采集` 浏览器插件。插件读取运营人员已登录的 1688 商品详情页中可见的标题、图片、SKU、价格、库存和属性，允许在发送前取消不想上架的 SKU，然后直接创建相同格式的第一阶段产物；这条路径不调用 AlphaShop。插件不会保存 ERP/1688 密码或 Cookie，也不会绕过登录、验证码、风控或访问权限。安装与使用说明见 [browser-extension/northstar-1688-collector/README.md](browser-extension/northstar-1688-collector/README.md)。

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
