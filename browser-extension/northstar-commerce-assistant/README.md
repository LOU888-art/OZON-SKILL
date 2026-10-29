# Northstar 跨境商品助手

一个 Chrome / Edge Manifest V3 插件，同时提供：

- 1688 商品详情、SKU、价格、库存、图片和运费采集，并发送至 Northstar ERP；
- Ozon 商品列表与详情页公开数据、跟卖价格浮层；
- 浏览器同时登录 Ozon Seller 时，补充月销量、销售额、流量、广告、转化和退货等授权指标；
- Ozon Seller 商品分析结果同步至 ERP 候选池。

安装时解压 ZIP，在 `chrome://extensions` 或 `edge://extensions` 开启开发者模式并加载 `northstar-commerce-assistant` 文件夹。要显示经营指标，请先在同一浏览器打开并登录 `https://seller.ozon.ru/`，再刷新 Ozon 商品页。插件只读取当前用户在页面上有权查看的数据，不保存平台密码或 Cookie，不绕过登录、验证或平台权限。
