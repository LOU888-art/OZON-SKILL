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

  function companyId(provided = "") {
    if (String(provided || "").trim()) return String(provided).trim();
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

  function mergePackaging(item, payload, sku) {
    if (!item || !payload) return item;
    const rows = objects(payload).filter(value => !Array.isArray(value));
    const source = rows.find(value => String(value.sku || value.product_id || value.id || "") === sku && (value.depth || value.width || value.height || value.attributes)) || rows.find(value => value.depth && value.width && value.height) || rows.find(value => Array.isArray(value.attributes));
    if (!source) return item;
    const result = { ...item };
    for (const key of ["depth", "width", "height", "weight", "sellerName", "seller_name"]) if (source[key] !== undefined) result[key] = source[key];
    const dimensions = [Number(source.depth), Number(source.width), Number(source.height)];
    if (dimensions.every(value => Number.isFinite(value) && value > 0)) result.packagingDimensions = `${dimensions.join(" × ")} mm`;
    if (Number(source.weight) > 0) result.packagingWeight = `${Number(source.weight)} g`;
    const attributes = Array.isArray(source.attributes) ? source.attributes : [];
    const byKey = key => Number(attributes.find(attribute => String(attribute?.key) === key)?.value || 0);
    const attrDimensions = [byKey("9454"), byKey("9455"), byKey("9456")];
    if (!result.packagingDimensions && attrDimensions.every(value => value > 0)) result.packagingDimensions = `${attrDimensions.join(" × ")} mm`;
    if (!result.packagingWeight && byKey("4497") > 0) result.packagingWeight = `${byKey("4497")} g`;
    return result;
  }

  async function enrichPackaging(item, sku, id) {
    try {
      const response = await fetch("https://seller.ozon.ru/api/v1/search", {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json", "X-O3-App-Name": "seller-ui", "X-O3-Company-Id": id, "X-O3-Language": "zh-Hans" },
        body: JSON.stringify({ company_id: id, need_total: true, filter: { children_nodes: { children_nodes: [{ input_leaf: { sku: { values: [sku] } } }], operator: "AND" } }, pagination: { limit: "50" }, is_copy_allowed: false }),
      });
      return response.ok ? mergePackaging(item, await response.json(), sku) : item;
    } catch (_) { return item; }
  }

  async function load(sku, force = false, providedCompanyId = "") {
    if (force) cache.delete(sku);
    if (cache.has(sku)) return cache.get(sku);
    const request = limited(() => loadUncached(sku, providedCompanyId));
    cache.set(sku, request);
    return request;
  }

  async function loadUncached(sku, providedCompanyId) {
    const id = companyId(providedCompanyId);
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
    return item ? { ok: true, data: await enrichPackaging(item, sku, id) } : { ok: false, error: "Seller 分析接口没有返回该 SKU" };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "NORTHSTAR_SELLER_METRICS") return false;
    load(String(message.sku || ""), Boolean(message.force), message.companyId).then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
  });
})();
