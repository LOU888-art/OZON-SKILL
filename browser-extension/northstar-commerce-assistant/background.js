const OZON_CACHE_KEY = "northstar_ozon_metrics_v1";
const OZON_CACHE_TTL = 30 * 60 * 1000;
let sellerQueue = Promise.resolve();
let lastSellerRequestAt = 0;

function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function limitedSellerRequest(job) {
  const run = async () => {
    const delay = Math.max(0, 1500 - (Date.now() - lastSellerRequestAt));
    if (delay) await wait(delay);
    lastSellerRequestAt = Date.now();
    return job();
  };
  const result = sellerQueue.then(run, run);
  sellerQueue = result.catch(() => {});
  return result;
}

async function cachedMetric(sku) {
  const stored = await chrome.storage.local.get(OZON_CACHE_KEY);
  const item = stored?.[OZON_CACHE_KEY]?.[sku];
  if (!item || Date.now() - Number(item.savedAt || 0) > OZON_CACHE_TTL) return null;
  return item.response || null;
}

async function saveMetric(sku, response) {
  if (!response?.ok) return;
  const stored = await chrome.storage.local.get(OZON_CACHE_KEY);
  const cache = stored?.[OZON_CACHE_KEY] && typeof stored[OZON_CACHE_KEY] === "object" ? stored[OZON_CACHE_KEY] : {};
  cache[sku] = { savedAt: Date.now(), response };
  for (const [key, item] of Object.entries(cache)) if (Date.now() - Number(item?.savedAt || 0) > OZON_CACHE_TTL) delete cache[key];
  await chrome.storage.local.set({ [OZON_CACHE_KEY]: cache });
}

async function executeInSellerTab(tabId, sku, suppliedCompanyId) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: async (requestedSku, providedCompanyId) => {
      const clean = value => String(value || "").trim();
      const cookie = document.cookie.match(/(?:^|;\s*)sc_company_id=([^;]+)/);
      const companyId = clean(providedCompanyId) || clean(localStorage.getItem("ozon_company_id")) || clean(sessionStorage.getItem("ozon_company_id")) || (cookie ? clean(decodeURIComponent(cookie[1])) : "");
      if (!companyId) return { ok: false, error: "Seller 已打开，但未识别到公司 ID；请重新登录 Seller" };
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 18000);
      try {
        const response = await fetch("https://seller.ozon.ru/api/site/seller-analytics/what_to_sell/data/v3", {
          method: "POST",
          credentials: "include",
          signal: controller.signal,
          headers: { "Content-Type": "application/json", "X-O3-App-Name": "seller-ui", "X-O3-Company-Id": companyId, "X-O3-Language": "zh-Hans", "X-O3-Page-Type": "analytics_platform" },
          body: JSON.stringify({ limit: "50", offset: "0", filter: { stock: "any_stock", period: "monthly", categories: [], sku: requestedSku }, sort: { key: "sum_missed_gmv_desc" } }),
        });
        if (!response.ok) return { ok: false, error: `Ozon Seller 接口 HTTP ${response.status}`, status: response.status };
        const payload = await response.json();
        const objects = [];
        const walk = (value, depth = 0) => {
          if (!value || typeof value !== "object" || depth > 9) return;
          if (!Array.isArray(value)) objects.push(value);
          for (const child of Object.values(value)) walk(child, depth + 1);
        };
        walk(payload);
        const exact = objects.find(item => String(item.sku || item.offer_id || item.product_id || item.id || "") === requestedSku);
        const informative = objects.filter(item => ["soldCount", "sold_count", "soldSum", "gmvSum", "qtyViewPdp"].some(key => item[key] !== undefined));
        const item = exact || (informative.length === 1 ? informative[0] : null);
        return item ? { ok: true, data: item } : { ok: false, error: "Ozon Seller 分析接口没有返回该 SKU" };
      } catch (error) {
        return { ok: false, error: error?.name === "AbortError" ? "Ozon Seller 请求超时" : (error?.message || String(error)) };
      } finally { clearTimeout(timer); }
    },
    args: [sku, suppliedCompanyId],
  });
  return result || { ok: false, error: "Seller 主环境没有返回数据" };
}

async function requestOzonMetrics(sku, force) {
  if (!force) {
    const cached = await cachedMetric(sku);
    if (cached) return { ...cached, source: "30 分钟本地缓存" };
  }
  const tabs = await chrome.tabs.query({ url: ["https://seller.ozon.ru/*", "https://seller.ozonru.cn/*"] });
  if (!tabs.length) return { ok: false, error: "请先在同一浏览器打开并登录 Ozon Seller" };
  const companyCookie = await chrome.cookies.get({ url: "https://seller.ozon.ru/", name: "sc_company_id" }).catch(() => null);
  const companyId = String(companyCookie?.value || "").trim();
  const orderedTabs = [...tabs].sort((left, right) => Number(/seller\.ozon\.ru\//.test(right.url || "")) - Number(/seller\.ozon\.ru\//.test(left.url || "")));
  let lastError = "Ozon Seller 未返回数据";
  for (const tab of orderedTabs) {
    try {
      const relayed = await chrome.tabs.sendMessage(tab.id, { type: "NORTHSTAR_SELLER_METRICS", sku, force, companyId });
      if (relayed?.ok) { await saveMetric(sku, relayed); return { ...relayed, source: "Ozon Seller 页面" }; }
      if (relayed?.error) lastError = relayed.error;
    } catch (_) {}
    try {
      const direct = await limitedSellerRequest(() => executeInSellerTab(tab.id, sku, companyId));
      if (direct?.ok) { await saveMetric(sku, direct); return { ...direct, source: "Ozon Seller 主环境" }; }
      if (direct?.error) lastError = direct.error;
    } catch (error) { lastError = error.message || String(error); }
  }
  return { ok: false, error: lastError };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NORTHSTAR_OZON_METRICS") return false;
  requestOzonMetrics(String(message.sku || ""), Boolean(message.force)).then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});
