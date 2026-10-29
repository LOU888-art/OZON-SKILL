chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NORTHSTAR_OZON_METRICS") return false;
  (async () => {
    const tabs = await chrome.tabs.query({ url: "https://seller.ozon.ru/*" });
    if (!tabs.length) return { ok: false, error: "请打开并登录 Ozon Seller 后台" };
    const companyCookie = await chrome.cookies.get({ url: "https://seller.ozon.ru/", name: "sc_company_id" }).catch(() => null);
    const companyId = String(companyCookie?.value || "").trim();
    let lastError = "Ozon Seller 未登录或无该商品分析权限";
    for (const tab of tabs) {
      try {
        const response = await chrome.tabs.sendMessage(tab.id, { type: "NORTHSTAR_SELLER_METRICS", sku: String(message.sku || ""), force: Boolean(message.force), companyId });
        if (response?.ok) return response;
        if (response?.error) lastError = response.error;
      } catch (_) {}
    }
    return { ok: false, error: lastError };
  })().then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});
