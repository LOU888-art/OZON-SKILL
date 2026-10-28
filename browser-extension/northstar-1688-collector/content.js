function cleanText(value) {
  if (value && typeof value === "object" && "textContent" in value) value = value.textContent;
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function absoluteUrl(value) {
  try {
    const raw = String(value || "").trim();
    if (!raw || raw.startsWith("data:") || raw.startsWith("blob:")) return "";
    const resolved = new URL(raw.startsWith("//") ? `https:${raw}` : raw, location.href);
    return resolved.protocol === "https:" ? resolved.href : "";
  } catch (_) { return ""; }
}

function numberValue(value) {
  if (typeof value === "number") return Number.isFinite(value) && value >= 0 ? value : null;
  const match = cleanText(value).replace(/[,，]/g, "").match(/(?:¥|￥)?\s*(\d+(?:\.\d+)?)/);
  return match ? Number(match[1]) : null;
}

function firstValue(object, keys) {
  for (const key of keys) if (object?.[key] !== undefined && object[key] !== null && object[key] !== "") return object[key];
  return null;
}

function offerId() {
  const match = location.href.match(/(?:offer\/|offerId=|offer_id=|itemId=|item_id=)(\d{5,})/i);
  return match?.[1] || "";
}

function addImage(target, value, width = null, height = null) {
  const url = absoluteUrl(typeof value === "object" ? (value?.url || value?.imageUrl || value?.imageURL || value?.originalUrl || value?.src) : value);
  if (!url || target.some(item => item.url === url)) return;
  target.push({ url, width: Number(width) || null, height: Number(height) || null });
}

function parsedJsonRoots() {
  const roots = [];
  for (const script of document.scripts) {
    const text = script.textContent?.trim() || "";
    if (!text || text.length > 8000000 || !/(?:sku|offer|product|price|freight|__NEXT|__INIT)/i.test(text)) continue;
    const attempts = [text];
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    if (start >= 0 && end > start) attempts.push(text.slice(start, end + 1));
    for (const candidate of attempts) {
      try {
        const value = JSON.parse(candidate);
        if (value && typeof value === "object") roots.push(value);
        break;
      } catch (_) {}
    }
  }
  return roots;
}

function requestMainCandidates() {
  return new Promise(resolve => {
    const nonce = crypto.randomUUID();
    const timer = setTimeout(() => { cleanup(); resolve({ candidates: [], response_urls: [] }); }, 1800);
    const listener = event => {
      if (event.source !== window || event.data?.source !== "northstar-1688-collector" || event.data?.type !== "SCAN_MAIN_RESULT" || event.data?.nonce !== nonce) return;
      cleanup();
      resolve({ candidates: Array.isArray(event.data.candidates) ? event.data.candidates : [], response_urls: Array.isArray(event.data.response_urls) ? event.data.response_urls : [] });
    };
    function cleanup() { clearTimeout(timer); window.removeEventListener("message", listener); }
    window.addEventListener("message", listener);
    window.postMessage({ source: "northstar-1688-collector", type: "SCAN_MAIN", nonce }, "*");
  });
}

function walk(roots, visitor) {
  const stack = [...roots];
  const seen = new Set();
  let visited = 0;
  while (stack.length && visited < 160000) {
    const value = stack.pop();
    if (!value || typeof value !== "object" || seen.has(value)) continue;
    seen.add(value); visited += 1; visitor(value);
    let children = [];
    try { children = Array.isArray(value) ? value : Object.values(value); } catch (_) { children = []; }
    for (const child of children) if (child && typeof child === "object") stack.push(child);
  }
}

const PRICE_KEYS = ["price", "skuPrice", "offerPrice", "discountPrice", "salePrice", "promotionPrice", "consignPrice", "unitPrice", "priceValue", "priceInYuan"];
const STOCK_KEYS = ["amountOnSale", "stock", "quantity", "canBookCount", "inventory", "sellableQuantity"];

function explicitPrice(object) {
  if (!object || typeof object !== "object") return null;
  const values = [];
  for (const key of PRICE_KEYS) {
    const raw = object[key];
    if (raw === undefined || raw === null || raw === "") continue;
    if (typeof raw === "object") {
      for (const nestedKey of ["value", "price", "priceText", "minPrice", "amount", "yuan"]) {
        const parsed = numberValue(raw[nestedKey]);
        if (parsed !== null && parsed > 0) values.push(parsed);
      }
    } else {
      const parsed = numberValue(raw);
      if (parsed !== null && parsed > 0) values.push(parsed);
    }
  }
  return values.length ? Math.min(...values) : null;
}

function explicitStock(object) {
  for (const key of STOCK_KEYS) {
    const parsed = numberValue(object?.[key]);
    if (parsed !== null) return Math.floor(parsed);
  }
  return null;
}

function specificationParts(raw, axisValues = new Map()) {
  const result = [];
  const add = (name, value, token = "") => {
    const tokenText = cleanText(token || value);
    const valueText = cleanText(value);
    const mapped = axisValues.get(tokenText) || axisValues.get(valueText) || axisValues.get(tokenText.split(":").pop());
    name = cleanText(name || mapped?.name); value = cleanText(mapped?.value || value);
    if (value && !result.some(item => item.name === (name || null) && item.value === value)) result.push({ name: name || null, value });
  };
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (item && typeof item === "object") add(firstValue(item, ["attributeDisplayName", "attributeName", "propName", "name", "key"]), firstValue(item, ["attributeValue", "valueName", "value", "text"]), firstValue(item, ["valueId", "vid", "id"]));
      else add(null, item, item);
    }
  } else if (raw && typeof raw === "object") {
    for (const [name, value] of Object.entries(raw)) if (typeof value !== "object") add(name, value, value);
  } else if (cleanText(raw)) {
    for (const token of cleanText(raw).replace(/^;+|;+$/g, "").split(/[;>|,]/).filter(Boolean)) {
      const [name, ...rest] = token.split(/[:：]/); const value = rest.join(":").trim();
      add(value ? name : null, value || name, token);
    }
  }
  return result.slice(0, 20);
}

function collectAxisValues(roots) {
  const result = new Map();
  walk(roots, object => {
    for (const key of ["skuProps", "skuProperties", "specProps", "saleProps", "productSKUPropertyList", "props"]) {
      const axes = object[key];
      if (!Array.isArray(axes)) continue;
      for (const axis of axes) {
        if (!axis || typeof axis !== "object") continue;
        const name = cleanText(firstValue(axis, ["prop", "propName", "name", "attributeName"]));
        const values = firstValue(axis, ["value", "values", "children", "propValues", "attributeValues"]);
        if (!Array.isArray(values)) continue;
        for (const item of values) {
          if (!item || typeof item !== "object") continue;
          const id = cleanText(firstValue(item, ["valueId", "vid", "id", "propValueId", "specId"]));
          const value = cleanText(firstValue(item, ["name", "value", "valueName", "text"]));
          const image = absoluteUrl(firstValue(item, ["imageUrl", "imageURL", "image", "picUrl"]));
          if (id && value) result.set(id, { name, value, image });
          if (value) result.set(value, { name, value, image });
        }
      }
    }
  });
  return result;
}

function skuFromObject(object, fallbackId, axisValues, rawSpec = null) {
  if (!object || typeof object !== "object") return null;
  const id = cleanText(firstValue(object, ["skuId", "skuID", "sku_id", "specId", "offerSkuId", "id"]) || fallbackId);
  const specRaw = firstValue(object, ["attributes", "skuAttributes", "specAttributes", "specification", "props", "propPath", "spec", "specId"]) || rawSpec;
  const specification = specificationParts(specRaw, axisValues);
  const price = explicitPrice(object); const stock = explicitStock(object);
  let image = absoluteUrl(firstValue(object, ["skuImageUrl", "imageUrl", "imageURL", "image", "picUrl"]));
  if (!image) image = specification.map(item => axisValues.get(item.value)?.image).find(Boolean) || "";
  const hasSignal = Boolean(firstValue(object, ["skuId", "skuID", "sku_id", "specId", "offerSkuId"])) || specification.length > 0;
  if (!hasSignal) return null;
  return { sku_id: id || `browser-sku-${Math.random().toString(36).slice(2, 10)}`, specification_zh: specification, specification_text_zh: specification.map(item => item.name ? `${item.name}:${item.value}` : item.value).join("; ") || null, purchase_price_cny: price, stock, image_url: image || null };
}

function collectStructuredSkus(roots, axisValues) {
  const groups = [];
  const mapKeyPattern = /^(?:skuMap|skuInfoMap|skuInfosMap|skuPriceMap|sku2info|sku2Info|skuData)$/i;
  walk(roots, object => {
    for (const [key, value] of Object.entries(object)) {
      if (mapKeyPattern.test(key) && value && typeof value === "object" && !Array.isArray(value)) {
        const entries = Object.entries(value);
        const group = [];
        for (const [specKey, item] of entries) {
          if (entries.length > 1 && specKey === "0") continue;
          const sku = skuFromObject(item, specKey, axisValues, specKey);
          if (sku) group.push(sku);
        }
        if (group.length) groups.push(group);
      }
      if (/^(?:skuInfos|skuInfoList|productSkuInfos|skus|skuList)$/i.test(key) && Array.isArray(value)) {
        const group = [];
        value.forEach((item, index) => { const sku = skuFromObject(item, `browser-sku-${index + 1}`, axisValues); if (sku) group.push(sku); });
        if (group.length) groups.push(group);
      }
    }
  });
  groups.sort((left, right) => {
    const score = group => group.reduce((total, sku) => total + 20 * Number(sku.purchase_price_cny !== null) + 5 * Number(sku.stock !== null) + 10 * sku.specification_zh.filter(item => item.name && !/^\d+$/.test(item.name)).length, 0) + Math.min(group.length, 100);
    return score(right) - score(left);
  });
  const skus = groups[0] || [];
  const merged = new Map();
  for (const sku of skus) {
    const key = cleanText(sku.sku_id) || sku.specification_text_zh;
    if (!key) continue;
    const existing = merged.get(key);
    const score = item => item.specification_zh.length + Number(item.purchase_price_cny !== null) + Number(item.stock !== null);
    if (!existing || score(sku) > score(existing)) merged.set(key, sku);
  }
  return [...merged.values()].slice(0, 500);
}

function collectAttributes(roots) {
  const result = {};
  const add = (name, value) => { name = cleanText(name); value = cleanText(value); if (name && value && name.length < 120 && Object.keys(result).length < 200) result[name] = value; };
  for (const row of document.querySelectorAll("table tr")) { const cells = [...row.querySelectorAll("th,td")].map(cleanText); if (cells.length >= 2) add(cells[0], cells.slice(1).join(" ")); }
  for (const list of document.querySelectorAll("dl")) { const names = [...list.querySelectorAll("dt")]; const values = [...list.querySelectorAll("dd")]; names.forEach((name, index) => add(name, values[index])); }
  for (const item of document.querySelectorAll("[class*='attribute'] li,[class*='property'] li,[class*='detail'] li")) { const parts = cleanText(item).split(/[:：]/); if (parts.length > 1) add(parts.shift(), parts.join(":")); }
  walk(roots, object => {
    for (const key of ["productAttributeList", "productAttribute", "attributes", "attributeList", "propertyList"]) {
      const raw = object[key];
      if (Array.isArray(raw)) {
        for (const item of raw) if (item && typeof item === "object") add(firstValue(item, ["attributeName", "name", "key", "propName"]), firstValue(item, ["attributeValue", "value", "values", "valueName"]));
      } else if (raw && typeof raw === "object") {
        for (const [name, value] of Object.entries(raw)) if (typeof value !== "object") add(name, value);
      }
    }
  });
  return result;
}

function collectTitle(roots) {
  const preferred = [document.querySelector("meta[property='og:title']")?.content, document.querySelector("h1[class*='title']"), document.querySelector("[class*='offer-title']"), document.querySelector("[class*='product-title']")].map(cleanText).filter(Boolean);
  const ranked = [];
  for (const element of document.querySelectorAll("[class*='title']")) {
    const value = cleanText(element);
    if (/[\u3400-\u9fff]/.test(value) && value.length >= 8 && value.length <= 200 && !/^(?:商品评价|商品属性|包装信息|商品详情)$/.test(value)) ranked.push({ score: 8, value });
  }
  walk(roots, object => {
    for (const [score, key] of [[10, "subject"], [10, "offerTitle"], [10, "productTitle"], [9, "productName"], [5, "title"]]) {
      const value = cleanText(object[key]); if (/[\u3400-\u9fff]/.test(value) && value.length >= 4 && value.length <= 500) ranked.push({ score, value });
    }
  });
  if (preferred.length) return preferred[0].replace(/[-_—|]\s*(?:阿里巴巴|1688).*$/i, "").trim();
  ranked.sort((a, b) => b.score - a.score || b.value.length - a.value.length);
  return ranked[0]?.value || cleanText(document.title).replace(/[-_—|]\s*(?:阿里巴巴|1688).*$/i, "").trim();
}

function collectImages(roots) {
  const main = []; const detail = [];
  addImage(main, document.querySelector("meta[property='og:image']")?.content);
  walk(roots, object => {
    for (const key of ["images", "mainImages", "imageList", "offerImages", "productImages"]) if (Array.isArray(object[key])) for (const image of object[key]) addImage(main, image, image?.width, image?.height);
    for (const key of ["detailImages", "descriptionImages"]) if (Array.isArray(object[key])) for (const image of object[key]) addImage(detail, image, image?.width, image?.height);
    for (const key of ["description", "detail", "descriptionHtml"]) {
      if (typeof object[key] !== "string" || !/<img/i.test(object[key])) continue;
      const parsed = new DOMParser().parseFromString(object[key], "text/html");
      for (const image of parsed.images) addImage(detail, image.getAttribute("src") || image.dataset.src || image.dataset.lazyload, Number(image.getAttribute("width")), Number(image.getAttribute("height")));
    }
  });
  for (const image of document.images) {
    const url = image.currentSrc || image.src || image.dataset.src || image.dataset.lazyload; const width = image.naturalWidth || Number(image.getAttribute("width")); const height = image.naturalHeight || Number(image.getAttribute("height"));
    const inDetail = Boolean(image.closest("[class*='detail'],[class*='description']"));
    if (inDetail && (width >= 200 || height >= 200)) addImage(detail, url, width, height); else if ((width >= 300 || height >= 300) && /alicdn|1688/i.test(url || "")) addImage(main, url, width, height);
  }
  return { main: main.slice(0, 40), detail: detail.slice(0, 120) };
}

function collectShipping(roots) {
  const body = cleanText(document.body?.innerText);
  const freightMatch = body.match(/(?:运费|物流费)\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*(起)?/); const routeMatch = body.match(/([\u3400-\u9fff]{2,12})\s*送至\s*([\u3400-\u9fff]{2,20})/); const deliveryMatch = body.match(/预计([^|]{2,40}?送达)/);
  let freight = freightMatch ? Number(freightMatch[1]) : null; let freightText = freightMatch ? freightMatch[0] : ""; let shipFrom = routeMatch?.[1] || ""; let destination = routeMatch?.[2] || "";
  walk(roots, object => {
    if (freight === null) { const raw = firstValue(object, ["freight", "freightFee", "shippingFee", "deliveryFee", "postFee", "logisticsFee"]); const parsed = numberValue(typeof raw === "object" ? firstValue(raw, ["value", "amount", "price", "fee"]) : raw); if (parsed !== null) { freight = parsed; freightText = cleanText(raw); } }
    if (!shipFrom) shipFrom = cleanText(firstValue(object, ["sendGoodsAddressText", "shipFrom", "sendAddress", "location"]));
    if (!destination) destination = cleanText(firstValue(object, ["destination", "receiveAddress", "deliveryAddress"]));
  });
  return { freight_cny: freight, freight_text_zh: freightText || null, ship_from_zh: shipFrom || null, destination_zh: destination || null, delivery_text_zh: deliveryMatch?.[1] || null };
}

function collectPriceRanges(roots) {
  const result = [];
  const add = (price, minimum, text) => {
    price = numberValue(price); minimum = numberValue(minimum);
    if (price === null || price <= 0 || result.some(item => item.price_cny === price && item.minimum_quantity === minimum)) return;
    result.push({ price_cny: price, minimum_quantity: minimum === null ? null : Math.floor(minimum), text_zh: cleanText(text) || null });
  };
  walk(roots, object => {
    for (const key of ["priceRange", "priceRanges", "priceRangeList", "rangePrices", "priceList", "priceScale"]) {
      const ranges = object[key];
      if (!Array.isArray(ranges)) continue;
      for (const item of ranges) if (item && typeof item === "object") add(firstValue(item, PRICE_KEYS), firstValue(item, ["beginAmount", "startQuantity", "minQuantity", "minimumQuantity", "quantity"]), JSON.stringify(item).slice(0, 300));
    }
  });
  const body = cleanText(document.body?.innerText);
  for (const match of body.matchAll(/[¥￥]\s*(\d+(?:\.\d+)?)\s+(\d+)\s*(?:件|个|瓶|套|只|支|盒|包|起)/g)) add(match[1], match[2], match[0]);
  return result.sort((left, right) => (left.minimum_quantity ?? Number.MAX_SAFE_INTEGER) - (right.minimum_quantity ?? Number.MAX_SAFE_INTEGER)).slice(0, 30);
}

function visibleSkuOptionCount() {
  const values = new Set();
  for (const element of document.querySelectorAll("[class*='sku'] [class*='item'],[class*='sku'] button,[class*='spec'] [class*='item'],[class*='prop'] [class*='item']")) { const text = cleanText(element.getAttribute("title") || element.getAttribute("aria-label") || element); if (text && text.length <= 60) values.add(text); }
  return values.size;
}

async function scan() {
  const mainData = await requestMainCandidates(); const scriptRoots = parsedJsonRoots(); const roots = [...mainData.candidates, ...scriptRoots];
  if (!roots.length) throw new Error("没有捕获到1688商品接口数据。请刷新当前详情页，等待价格和规格显示后重新采集");
  const axisValues = collectAxisValues(roots); let skuList = collectStructuredSkus(roots, axisValues); const images = collectImages(roots); const shipping = collectShipping(roots); const priceRanges = collectPriceRanges(roots); const warnings = []; const visibleOptions = visibleSkuOptionCount();
  if (!skuList.length && visibleOptions <= 1) { const priceText = cleanText(document.querySelector("[class*='price']")); skuList = [{ sku_id: `${offerId()}-default`, specification_zh: [], specification_text_zh: "默认规格", purchase_price_cny: priceRanges[0]?.price_cny ?? numberValue(priceText), stock: null, image_url: images.main[0]?.url || null }]; }
  if (!skuList.length || (skuList.length === 1 && visibleOptions > 1)) throw new Error(`页面显示多个规格，但只读取到 ${skuList.length} 个SKU。请刷新详情页，等待规格和价格加载完成后重新采集`);
  const missingPrices = skuList.filter(sku => sku.purchase_price_cny === null).length;
  if (missingPrices) warnings.push(`${missingPrices}个SKU没有读取到采购价，发送前请刷新页面重试`);
  if (missingPrices === skuList.length) throw new Error("没有读取到任何SKU采购价。请刷新1688详情页，等待价格加载完成后重试");
  const title = collectTitle(roots); if (!title) throw new Error("没有识别到商品标题，请等待页面加载完成后重试"); if (!images.main.length) throw new Error("没有识别到商品主图，请等待页面加载完成后重试");
  const attributes = collectAttributes(roots); let categoryId = ""; let supplierName = "";
  walk(roots, object => { if (!categoryId) { const value = cleanText(firstValue(object, ["thirdCategoryId", "categoryId", "categoryID", "leafCategoryId"])); if (/^\d+$/.test(value)) categoryId = value; } if (!supplierName) supplierName = cleanText(firstValue(object, ["companyName", "supplierName", "shopName"])); });
  const breadcrumbs = [...document.querySelectorAll("[class*='breadcrumb'] a,[class*='crumb'] a")].map(cleanText).filter(Boolean).slice(0, 20);
  return { source_offer_id: offerId(), product_url: location.href, title_zh: title, images: images.main, detail_images: images.detail, sku_list: skuList, price_ranges: priceRanges, attributes_zh: attributes, category_id: categoryId, category_path_zh: breadcrumbs, supplier_info: { name: supplierName || null, min_order: priceRanges.find(item => item.minimum_quantity !== null)?.minimum_quantity ?? null, ship_from: shipping.ship_from_zh }, shipping_info: shipping, capture_diagnostics: { collector_version: "0.2.0", main_candidate_count: mainData.candidates.length, intercepted_response_count: mainData.response_urls.length, visible_sku_option_count: visibleOptions, warnings }, captured_at: new Date().toISOString() };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NORTHSTAR_1688_SCAN") return false;
  scan().then(product => sendResponse({ ok: true, product })).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});
