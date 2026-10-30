const $ = selector => document.querySelector(selector);
const setStatus = (message, type = "") => { $("#status").textContent = message; $("#status").className = type; };
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
let captured = null;
let saveErpUrlTimer = null;

function normalizedUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("ERP必须使用HTTPS地址");
  return url.origin;
}

function originPattern(origin) { return `${origin}/*`; }

function showErpUrlSaved(saved) {
  const hint = $("#erpUrlHint");
  hint.textContent = saved ? "已记住，下次打开插件自动填充" : "首次填写后自动记住";
  hint.classList.toggle("saved", saved);
}

async function saveErpUrl() {
  const input = $("#erpUrl");
  const value = input.value.trim();
  if (!value) {
    await chrome.storage.local.remove("erpUrl");
    showErpUrlSaved(false);
    return "";
  }
  const origin = normalizedUrl(value);
  await chrome.storage.local.set({ erpUrl: origin });
  input.value = origin;
  showErpUrlSaved(true);
  return origin;
}

function scheduleErpUrlSave() {
  clearTimeout(saveErpUrlTimer);
  saveErpUrlTimer = setTimeout(() => {
    saveErpUrl().catch(() => showErpUrlSaved(false));
  }, 500);
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

function render() {
  const product = captured;
  $("#preview").classList.remove("hidden");
  $("#productImage").src = product.images?.[0]?.url || "";
  $("#productTitle").textContent = product.title_zh || "未识别标题";
  $("#productMeta").textContent = `商品ID ${product.source_offer_id || "未识别"} · 主图 ${product.images?.length || 0} 张 · 详情图 ${product.detail_images?.length || 0} 张`;
  const shipping = product.shipping_info || {};
  const warnings = product.capture_diagnostics?.warnings || [];
  const groupSummary = (product.capture_diagnostics?.spec_groups || []).map(group => `${group.name}${group.count}项`).join(" × ");
  const summary = [
    `完整SKU ${product.sku_list?.length || 0} 个`,
    groupSummary || null,
    `阶梯价 ${product.price_ranges?.length || 0} 档`,
    shipping.freight_cny !== null && shipping.freight_cny !== undefined ? `运费 ¥${shipping.freight_cny}${shipping.freight_text_zh?.includes("起") ? "起" : ""}` : "运费未读取",
    shipping.ship_from_zh ? `发货地 ${shipping.ship_from_zh}` : "发货地未读取",
  ];
  $("#captureSummary").textContent = [...summary.filter(Boolean), ...warnings].join(" · ");
  $("#captureSummary").classList.toggle("warning", warnings.length > 0 || shipping.freight_cny === null || shipping.freight_cny === undefined);
  $("#skuList").innerHTML = (product.sku_list || []).map((sku, index) => `<label class="sku"><input type="checkbox" data-sku-index="${index}" checked><img src="${escapeHtml(sku.image_url || product.images?.[0]?.url || "")}" alt=""><span><b>${escapeHtml(sku.specification_text_zh || sku.sku_id)}</b><small>SKU ${escapeHtml(sku.sku_id)} · ¥${escapeHtml(sku.purchase_price_cny ?? "未读取")} · 库存 ${escapeHtml(sku.stock ?? "未知")}</small></span></label>`).join("");
  document.querySelectorAll("[data-sku-index]").forEach(input => { input.onchange = updateCount; });
  updateCount();
}

function updateCount() {
  const controls = [...document.querySelectorAll("[data-sku-index]")];
  const selected = controls.filter(item => item.checked).length;
  $("#skuCount").textContent = `保留 ${selected}/${controls.length}`;
  $("#selectAll").checked = Boolean(controls.length) && selected === controls.length;
  $("#selectAll").indeterminate = selected > 0 && selected < controls.length;
  $("#sendBtn").disabled = selected === 0;
}

async function scanPage(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "NORTHSTAR_1688_SCAN" });
  } catch (_) {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", files: ["main-probe.js"] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    return chrome.tabs.sendMessage(tabId, { type: "NORTHSTAR_1688_SCAN" });
  }
}

async function scan() {
  const button = $("#scanBtn");
  button.disabled = true;
  try {
    if ($("#erpUrl").value.trim()) await saveErpUrl();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url || !/https:\/\/[^/]*1688\.com\//i.test(tab.url)) throw new Error("当前标签页不是1688商品页面");
    setStatus("正在读取页面接口、完整SKU、价格、库存与运费…");
    const response = await scanPage(tab.id);
    if (!response?.ok) throw new Error(response?.error || "页面采集失败");
    captured = response.product;
    if (captured?.capture_diagnostics?.collector_version !== "0.3.7") throw new Error("插件已更新，请先刷新当前1688商品页，再重新读取");
    if (!captured?.source_offer_id) throw new Error("没有识别到商品ID，请打开 detail.1688.com 的商品详情页");
    render();
    setStatus("采集完成。请核对SKU数量、价格和运费，取消不想上架的SKU后再发送。", "success");
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally { button.disabled = false; }
}

async function send() {
  const button = $("#sendBtn");
  button.disabled = true;
  try {
    const missingPrices = captured.sku_list.filter(sku => sku.purchase_price_cny === null || sku.purchase_price_cny === undefined);
    if (missingPrices.length) throw new Error(`仍有${missingPrices.length}个SKU没有采购价，请刷新1688页面后重新采集`);
    const rawErpUrl = $("#erpUrl").value.trim();
    if (!rawErpUrl) throw new Error("请先填写ERP网址");
    const origin = normalizedUrl(rawErpUrl);
    const granted = await chrome.permissions.request({ origins: [originPattern(origin)] });
    if (!granted) throw new Error("未授权插件访问ERP网址");
    await saveErpUrl();
    const selected = [...document.querySelectorAll("[data-sku-index]")].filter(input => input.checked).map(input => captured.sku_list[Number(input.dataset.skuIndex)]);
    if (!selected.length) throw new Error("至少保留一个SKU");
    const product = { ...captured, sku_list: selected, capture_diagnostics: { ...(captured.capture_diagnostics || {}), captured_sku_count: captured.sku_list.length, selected_sku_count: selected.length } };
    setStatus("正在发送完整商品数据到ERP第一阶段…");
    const result = await erpCall(origin, "/api/collector/1688", { method: "POST", body: JSON.stringify({ collector_version: "0.3.7", product }) });
    setStatus(`采集成功：商品 ${result.pipeline_id}，${result.sku_count} 个SKU、${result.image_count} 张主图，运费信息已保存。`, "success");
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally { button.disabled = false; }
}

document.addEventListener("DOMContentLoaded", async () => {
  const saved = await chrome.storage.local.get("erpUrl");
  $("#erpUrl").value = saved.erpUrl || "";
  showErpUrlSaved(Boolean(saved.erpUrl));
  $("#erpUrl").oninput = scheduleErpUrlSave;
  $("#erpUrl").onchange = () => {
    clearTimeout(saveErpUrlTimer);
    saveErpUrl().catch(error => setStatus(error.message || String(error), "error"));
  };
  $("#scanBtn").onclick = scan;
  $("#sendBtn").onclick = send;
  $("#selectAll").onchange = event => { document.querySelectorAll("[data-sku-index]").forEach(input => { input.checked = event.target.checked; }); updateCount(); };
});
