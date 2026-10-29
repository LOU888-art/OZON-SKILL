(() => {
  if (window.__northstarSellerMetricsInstalled) return;
  window.__northstarSellerMetricsInstalled = true;
  const cache = new Map();
  let queue = Promise.resolve();
  let lastRequestAt = 0;

  function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

  function limited(job) {
    const run = async () => {
      const delay = Math.max(0, 1400 - (Date.now() - lastRequestAt));
      if (delay) await wait(delay);
      lastRequestAt = Date.now();
      return job();
    };
    const result = queue.then(run, run);
    queue = result.catch(() => {});
    return result;
  }

  function companyId() {
    const values = [];
    try { values.push(localStorage.getItem("ozon_company_id")); } catch (_) {}
    try { values.push(sessionStorage.getItem("ozon_company_id")); } catch (_) {}
    const cookie = document.cookie.match(/(?:^|;\s*)sc_company_id=([^;]+)/);
    if (cookie) values.push(decodeURIComponent(cookie[1]));
    return values.map(value => String(value || "").trim()).find(Boolean) || "";
  }

  function objects(value, output = [], depth = 0) {
    if (!value || typeof value !== "object" || depth > 9) return output;
    output.push(value);
    for (const child of Object.values(value)) objects(child, output, depth + 1);
    return output;
  }

  function bestItem(payload, sku) {
    const candidates = objects(payload).filter(item => !Array.isArray(item));
    const exact = candidates.find(item => String(item.sku || item.offer_id || item.product_id || item.id || "") === sku);
    if (exact) return exact;
    const informative = candidates.filter(item => ["soldCount", "sold_count", "soldSum", "gmvSum", "qtyViewPdp"].some(key => item[key] !== undefined));
    return informative.length === 1 ? informative[0] : null;
  }

  async function load(sku, force = false) {
    if (force) cache.delete(sku);
    if (cache.has(sku)) return cache.get(sku);
    const request = limited(() => loadUncached(sku));
    cache.set(sku, request);
    return request;
  }

  async function loadUncached(sku) {
    const id = companyId();
    if (!id) return { ok: false, error: "请先登录 Ozon Seller，未识别到公司 ID" };
    const response = await fetch("https://seller.ozon.ru/api/site/seller-analytics/what_to_sell/data/v3", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-O3-App-Name": "seller-ui", "X-O3-Company-Id": id, "X-O3-Language": "zh-Hans", "X-O3-Page-Type": "analytics_platform" },
      body: JSON.stringify({ limit: "50", offset: "0", filter: { stock: "any_stock", period: "monthly", categories: [], sku }, sort: { key: "sum_missed_gmv_desc" } }),
    });
    if (!response.ok) {
      cache.delete(sku);
      return { ok: false, error: `Ozon Seller 接口 HTTP ${response.status}` };
    }
    const item = bestItem(await response.json(), sku);
    return item ? { ok: true, data: item } : { ok: false, error: "Seller 分析接口没有返回该 SKU" };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "NORTHSTAR_SELLER_METRICS") return false;
    load(String(message.sku || ""), Boolean(message.force)).then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
  });
})();
