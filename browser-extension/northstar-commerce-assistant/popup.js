const $ = selector => document.querySelector(selector);
const ERP_ORIGIN = "https://8.217.125.205";
const setStatus = (message, type = "") => { $("#status").textContent = message; $("#status").className = type; };
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
let captured = null;

function normalizedUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("ERP必须使用HTTPS地址");
  return url.origin;
}

function originPattern(origin) { return `${origin}/*`; }

async function saveErpUrl() {
  const value = $("#erpUrl").value.trim();
  if (!value) {
    await chrome.storage.local.remove("erpUrl");
    $("#erpHint").textContent = "首次填写后自动记住";
    return "";
  }
  const origin = normalizedUrl(value);
  await chrome.storage.local.set({ erpUrl: origin });
  $("#erpUrl").value = origin;
  $("#erpHint").textContent = "已记住，下次打开自动填充";
  return origin;
}

async function erpCall(origin, path, options = {}) {
  const tabs = await chrome.tabs.query({ url: originPattern(origin) });
  if (!tabs.length) throw new Error("请先在浏览器中打开并登录ERP页面");
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tabs[0].id },
    func: async (requestPath, requestOptions) => {
      const response = await fetch(requestPath, { ...requestOptions, credentials: "same-origin", headers: { "Content-Type": "application/json", ...(requestOptions.headers || {}) } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    },
    args: [path, options],
  });
  return result;
}

function showMode(mode) {
  document.querySelectorAll(".mode").forEach(node => node.classList.add("hidden"));
  $(`#mode${mode}`).classList.remove("hidden");
  const names = { "1688": "1688采集", Seller: "Ozon Seller", Ozon: "Ozon数据", Unknown: "未支持页面" };
  $("#modeBadge").textContent = names[mode];
}

function updateSkuCount() {
  const controls = [...document.querySelectorAll("[data-sku-index]")];
  const selected = controls.filter(item => item.checked).length;
  $("#skuCount").textContent = `保留 ${selected}/${controls.length}`;
  $("#selectAll").checked = Boolean(controls.length) && selected === controls.length;
  $("#selectAll").indeterminate = selected > 0 && selected < controls.length;
  $("#send1688Btn").disabled = selected === 0;
}

function render1688() {
  const product = captured;
  $("#preview1688").classList.remove("hidden");
  $("#productImage").src = product.images?.[0]?.url || "";
  $("#productTitle").textContent = product.title_zh || "未识别标题";
  $("#productMeta").textContent = `商品ID ${product.source_offer_id || "未识别"} · 主图 ${product.images?.length || 0} 张 · 详情图 ${product.detail_images?.length || 0} 张`;
  const shipping = product.shipping_info || {};
  const groups = (product.capture_diagnostics?.spec_groups || []).map(group => `${group.name}${group.count}项`).join(" × ");
  $("#captureSummary").textContent = [
    `完整SKU ${product.sku_list?.length || 0} 个`,
    groups || null,
    `阶梯价 ${product.price_ranges?.length || 0} 档`,
    shipping.freight_cny !== null && shipping.freight_cny !== undefined ? `运费 ¥${shipping.freight_cny}` : "运费未读取",
    shipping.ship_from_zh ? `发货地 ${shipping.ship_from_zh}` : "发货地未读取",
  ].filter(Boolean).join(" · ");
  $("#skuList").innerHTML = (product.sku_list || []).map((sku, index) => `<label class="sku"><input type="checkbox" data-sku-index="${index}" checked><img src="${escapeHtml(sku.image_url || product.images?.[0]?.url || "")}" alt=""><span><b>${escapeHtml(sku.specification_text_zh || sku.sku_id)}</b><small>SKU ${escapeHtml(sku.sku_id)} · ¥${escapeHtml(sku.purchase_price_cny ?? "未读取")} · 库存 ${escapeHtml(sku.stock ?? "未知")}</small></span></label>`).join("");
  document.querySelectorAll("[data-sku-index]").forEach(input => { input.onchange = updateSkuCount; });
  updateSkuCount();
}

async function scan1688Page(tabId) {
  try { return await chrome.tabs.sendMessage(tabId, { type: "NORTHSTAR_1688_SCAN" }); }
  catch (_) {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", files: ["collector-main-probe.js"] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["collector-content.js"] });
    return chrome.tabs.sendMessage(tabId, { type: "NORTHSTAR_1688_SCAN" });
  }
}

async function scan1688() {
  const button = $("#scan1688Btn"); button.disabled = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url || !/https:\/\/[^/]*1688\.com\//i.test(tab.url)) throw new Error("当前标签页不是1688商品页面");
    setStatus("正在读取完整SKU、价格、库存与运费…");
    const response = await scan1688Page(tab.id);
    if (!response?.ok) throw new Error(response?.error || "页面采集失败");
    captured = response.product;
    if (captured?.capture_diagnostics?.collector_version !== "0.3.5") throw new Error("插件已更新，请刷新1688商品页后重新读取");
    render1688();
    setStatus("采集完成。核对SKU后发送到ERP。", "success");
  } catch (error) { setStatus(error.message || String(error), "error"); }
  finally { button.disabled = false; }
}

async function send1688() {
  const button = $("#send1688Btn"); button.disabled = true;
  try {
    if (!captured) throw new Error("请先读取当前1688商品");
    const missing = captured.sku_list.filter(sku => sku.purchase_price_cny === null || sku.purchase_price_cny === undefined);
    if (missing.length) throw new Error(`仍有${missing.length}个SKU没有采购价，请刷新页面后重试`);
    const origin = ERP_ORIGIN;
    const selected = [...document.querySelectorAll("[data-sku-index]")].filter(input => input.checked).map(input => captured.sku_list[Number(input.dataset.skuIndex)]);
    if (!selected.length) throw new Error("至少保留一个SKU");
    const product = { ...captured, sku_list: selected, capture_diagnostics: { ...(captured.capture_diagnostics || {}), captured_sku_count: captured.sku_list.length, selected_sku_count: selected.length } };
    setStatus("正在发送到ERP第一阶段…");
    const result = await erpCall(origin, "/api/collector/1688", { method: "POST", body: JSON.stringify({ collector_version: "0.3.5", product }) });
    setStatus(`采集成功：商品 ${result.pipeline_id}，保留 ${result.sku_count} 个SKU。`, "success");
  } catch (error) { setStatus(error.message || String(error), "error"); }
  finally { button.disabled = false; }
}

async function readRules(origin) {
  const data = await erpCall(origin, "/api/research");
  if (!data.rules) throw new Error("请先在ERP的“Ozon选品”页面保存规则");
  $("#ruleInfo").innerHTML = `<b>选品规则</b><p>${escapeHtml(data.rules.date_from)} 至 ${escapeHtml(data.rules.date_to)} · 最多 ${escapeHtml(data.rules.max_candidates)} 个</p>`;
  return data.rules;
}

async function syncSeller() {
  const button = $("#syncSellerBtn"); button.disabled = true;
  try {
    const origin = ERP_ORIGIN;
    const rules = await readRules(origin);
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url?.startsWith("https://seller.ozon.ru/")) throw new Error("当前标签页不是Ozon Seller后台");
    setStatus("正在读取Ozon Seller商品分析结果…");
    const response = await chrome.tabs.sendMessage(tab.id, { type: "NORTHSTAR_SCAN", rules, autoScroll: $("#autoScroll").checked, limit: Number($("#scanLimit").value) });
    if (!response?.ok) throw new Error(response?.error || "无法读取Ozon页面");
    if (!response.items.length) throw new Error("当前页面没有识别到商品行");
    const result = await erpCall(origin, "/api/research/ingest", { method: "POST", body: JSON.stringify({ items: response.items }) });
    setStatus(`同步完成：候选池 ${result.accepted} 个，规则排除 ${result.rejected} 个。`, "success");
  } catch (error) { setStatus(error.message || String(error), "error"); }
  finally { button.disabled = false; }
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#scan1688Btn").onclick = scan1688;
  $("#send1688Btn").onclick = send1688;
  $("#syncSellerBtn").onclick = syncSeller;
  $("#selectAll").onchange = event => { document.querySelectorAll("[data-sku-index]").forEach(input => { input.checked = event.target.checked; }); updateSkuCount(); };
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url || "";
  if (/https:\/\/[^/]*1688\.com\//i.test(url)) { showMode("1688"); setStatus("读取当前1688商品并发送到ERP。"); }
  else if (url.startsWith("https://seller.ozon.ru/")) { showMode("Seller"); setStatus("可扫描Ozon Seller当前分析结果。"); }
  else if (/^https:\/\/(?:www\.)?ozon\.ru\//i.test(url)) { showMode("Ozon"); setStatus("Ozon页面数据浮层已自动启用。", "success"); }
  else { showMode("Unknown"); setStatus("请打开受支持的商品页面。", "error"); }
});
