const OZON_CACHE_KEY = "northstar_ozon_metrics_v2";
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
        let item = exact || (informative.length === 1 ? informative[0] : null);
        if (!item) return { ok: false, error: "Ozon Seller 分析接口没有返回该 SKU" };
        try {
          const variantResponse = await fetch("https://seller.ozon.ru/api/v1/search", {
            method: "POST", credentials: "include",
            headers: { "Content-Type": "application/json", "X-O3-App-Name": "seller-ui", "X-O3-Company-Id": companyId, "X-O3-Language": "zh-Hans" },
            body: JSON.stringify({ company_id: companyId, need_total: true, filter: { children_nodes: { children_nodes: [{ input_leaf: { sku: { values: [requestedSku] } } }], operator: "AND" } }, pagination: { limit: "50" }, is_copy_allowed: false }),
          });
          if (variantResponse.ok) {
            const variantObjects = [];
            walk(await variantResponse.json());
            variantObjects.push(...objects.splice(0));
            const variant = variantObjects.find(value => String(value.sku || value.product_id || value.id || "") === requestedSku && (value.depth || value.width || value.height || value.attributes)) || variantObjects.find(value => value.depth && value.width && value.height) || variantObjects.find(value => Array.isArray(value.attributes));
            if (variant) {
              item = { ...item };
              for (const key of ["depth", "width", "height", "weight", "sellerName", "seller_name"]) if (variant[key] !== undefined) item[key] = variant[key];
              const dimensions = [Number(variant.depth), Number(variant.width), Number(variant.height)];
              if (dimensions.every(value => Number.isFinite(value) && value > 0)) item.packagingDimensions = `${dimensions.join(" × ")} mm`;
              if (Number(variant.weight) > 0) item.packagingWeight = `${Number(variant.weight)} g`;
            }
          }
        } catch (_) {}
        return { ok: true, data: item };
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

async function lookupErpCommission(category, price) {
  const tabs = await chrome.tabs.query({ url: "https://8.217.125.205/*" });
  if (!tabs.length || !String(category || "").trim()) return { ok: false };
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tabs[0].id },
    func: async query => {
      const response = await fetch(`/api/pricing/categories?q=${encodeURIComponent(query)}&limit=30`, { credentials: "same-origin" });
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) return { ok: false };
      const payload = await response.json();
      const normalized = String(query).trim().toLowerCase();
      const rows = Array.isArray(payload.results) ? payload.results : [];
      const exact = rows.find(row => [row.type_zh, row.type_ru].some(value => String(value || "").trim().toLowerCase() === normalized));
      if (!exact || !Array.isArray(exact.rates)) return { ok: false };
      return { ok: true, rates: exact.rates, category: `${exact.type_zh} / ${exact.type_ru}`, effectiveFrom: payload.effective_from || "" };
    },
    args: [String(category).trim()],
  });
  if (!result?.ok) return { ok: false };
  const index = Number(price) <= 1500 ? 0 : (Number(price) <= 5000 ? 1 : 2);
  return { ...result, rate: Number(result.rates[index]) };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NORTHSTAR_ERP_COMMISSION") return false;
  lookupErpCommission(message.category, message.price).then(sendResponse).catch(() => sendResponse({ ok: false }));
  return true;
});

async function send1688ToErp(product) {
  const tabs = await chrome.tabs.query({ url: "https://8.217.125.205/*" });
  if (!tabs.length) {
    await chrome.tabs.create({ url: "https://8.217.125.205/login", active: true });
    return { ok: false, error: "已打开 ERP 登录页。登录后回到1688页面再次发送" };
  }
  const target = tabs.find(tab => tab.active) || tabs[0];
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: target.id },
    world: "MAIN",
    func: async payload => {
      const response = await fetch("/api/collector/1688", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ collector_version: "0.3.9", product: payload }),
      });
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) return { ok: false, error: "ERP 登录已失效，请重新登录" };
      const data = await response.json().catch(() => ({}));
      if (!response.ok) return { ok: false, error: data.error || `ERP HTTP ${response.status}` };
      window.dispatchEvent(new CustomEvent("northstar:collector-complete", { detail: { pipeline_id: data.pipeline_id || "", sku_count: data.sku_count || 0 } }));
      return { ok: true, data };
    },
    args: [product],
  });
  return result || { ok: false, error: "ERP 没有返回采集结果" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NORTHSTAR_1688_SEND") return false;
  send1688ToErp(message.product).then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});
