# Northstar Ozon 自动选品插件

Chrome / Edge Manifest V3 插件。它在用户已登录的 `seller.ozon.ru` 商品分析页面读取当前表格，通过已登录的 Northstar ERP 标签页调用同源接口，因此不保存 ERP 密码、Ozon 密码或 Cookie。

## 本地安装

1. 打开 `chrome://extensions` 或 `edge://extensions`。
2. 开启开发者模式。
3. 选择“加载已解压的扩展程序”。
4. 选择本目录 `browser-extension/northstar-ozon-research`。
5. 在 ERP 的“Ozon 选品”页面保存选品规则。
6. 同时打开 ERP 和 Ozon Seller 商品分析页面，点击插件图标同步。

不需要打开开发者工具，也不依赖 F12。若要排查页面字段识别，可用 `Ctrl+Shift+I`（macOS 为 `Cmd+Option+I`）打开浏览器开发者工具。

## 当前边界

- 插件会读取当前账号有权查看、并已加载到页面表格中的商品；勾选自动滚动后最多滚动 24 次，单次最多同步 500 条。
- 日期会尝试写入页面原生日期控件；Ozon 使用自定义日期组件时，请先在页面选择日期。无论页面控件是否可写，ERP 都会按保存的日期和指标规则记录、复核同步数据。
- Ozon 页面结构会变化，插件采用中/俄/英表头别名识别，无法识别的指标不会编造；当规则要求该指标时，服务端会排除缺少指标的商品。
- 当前版本不会把 Ozon 密码、ERP 密码或 Cookie 保存到扩展存储，也不会绕过登录权限。

插件只读取 Ozon 当前账户有权看到且已加载到页面中的结果。Ozon 页面结构变化时，需更新 `content.js` 的字段识别别名。
