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

function highResolutionImageUrl(value) {
  const resolved = absoluteUrl(value);
  if (!resolved) return "";
  try {
    const parsed = new URL(resolved);
    if (!/(?:alicdn|alibaba|1688)\.com$/i.test(parsed.hostname)) return resolved;
    // 1688 option buttons normally expose a tiny CDN derivative such as
    // `photo.jpg_60x60.jpg` or `photo.jpg_100x100q90.jpg_.webp`.  Everything
    // after the first real image extension is a resize/format instruction;
    // removing it returns the supplier's original image instead of enlarging
    // a blurry thumbnail inside ERP.
    const extension = parsed.pathname.match(/\.(?:jpe?g|png|webp|gif|avif|heic)/i);
    if (extension?.index !== undefined) {
      parsed.pathname = parsed.pathname.slice(0, extension.index + extension[0].length);
      parsed.search = "";
      parsed.hash = "";
    }
    return parsed.href;
  } catch (_error) {
    return resolved;
  }
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

const PRICE_KEYS = ["price", "skuPrice", "offerPrice", "discountPrice", "currentPrice", "priceDisplay", "salePrice", "promotionPrice", "consignPrice", "unitPrice", "priceValue", "priceInYuan"];
const STOCK_KEYS = ["amountOnSale", "stock", "quantity", "canBookCount", "canBookedAmount", "inventory", "sellableQuantity"];

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
        const name = cleanText(firstValue(axis, ["prop", "propName", "name", "attributeName", "title"]));
        const values = firstValue(axis, ["value", "values", "children", "propValues", "attributeValues"]);
        if (!Array.isArray(values)) continue;
        for (const item of values) {
          if (!item || typeof item !== "object") continue;
          const id = cleanText(firstValue(item, ["valueId", "vid", "id", "propValueId", "specId"]));
          const value = cleanText(firstValue(item, ["name", "value", "valueName", "text"]));
          const rawImage = firstValue(item, ["imageUrl", "imageURL", "image", "picUrl", "picURL", "imageURI", "imageUri", "imagePath", "skuImage", "skuImageUrl", "fullPathImageURI", "size310x310ImageURI", "size220x220ImageURI"]);
          const image = absoluteUrl(rawImage && typeof rawImage === "object" ? firstValue(rawImage, ["url", "src", "imageUrl", "imageURL", "originalUrl"]) : rawImage);
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
  const specRaw = firstValue(object, ["attributes", "skuAttributes", "specAttributes", "specAttrs", "specAttr", "specification", "props", "propPath", "spec", "specId"]) || rawSpec;
  const specification = specificationParts(specRaw, axisValues);
  const price = explicitPrice(object); const stock = explicitStock(object);
  let image = absoluteUrl(firstValue(object, ["skuImageUrl", "skuImage", "imageUrl", "imageURL", "imageURI", "imageUri", "imagePath", "image", "picUrl", "picURL", "fullPathImageURI", "size310x310ImageURI", "size220x220ImageURI"]));
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
      if (mapKeyPattern.test(key) && Array.isArray(value)) {
        const group = [];
        value.forEach((item, index) => {
          const sku = skuFromObject(item, `structured-sku-${index + 1}`, axisValues);
          if (sku) group.push(sku);
        });
        if (group.length) groups.push(group);
      }
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
    for (const key of ["priceRange", "priceRanges", "priceRangeList", "rangePrices", "priceList", "priceScale", "offerPriceRanges", "currentPrices", "disPriceRanges"]) {
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

function visibleSkuElements() {
  const selector = "[class*='sku'] [class*='item'],[class*='sku'] button,[class*='spec'] [class*='item'],[class*='prop'] [class*='item']";
  const raw = [...document.querySelectorAll(selector)].filter(element => {
    const text = cleanText(element.getAttribute("title") || element.getAttribute("aria-label") || element);
    const rect = element.getBoundingClientRect();
    return text && text.length <= 60 && !/^(?:\+|-|−|立即下单|加采购车|跨境铺货|立即询价)$/.test(text) && rect.width > 0 && rect.height > 0 && !element.matches(":disabled,[aria-disabled='true']");
  });
  return raw.filter(element => !raw.some(other => other !== element && element.contains(other)));
}

const SKU_AXIS_NAMES = new Set(["颜色", "颜色分类", "香型", "净含量", "规格", "尺寸", "尺码", "型号", "款式", "类型", "口味", "容量", "重量", "长度", "直径", "材质", "包装", "数量", "套装", "适用对象"]);

function isVisibleElement(element) {
  if (!element?.getBoundingClientRect) return false;
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function validOptionLabel(value, axisName) {
  const text = cleanText(value);
  if (!text || text.length > 50 || text === axisName || SKU_AXIS_NAMES.has(text)) return false;
  if (/^(?:0|\+|-|−|立即下单|加采购车|跨境铺货|立即询价)$/.test(text)) return false;
  if (/[¥￥]|库存|起批|已售|采购|询价|运费|送达/.test(text)) return false;
  return true;
}

function directOptionLabels(container, axisName) {
  let current = container;
  for (let depth = 0; current && depth < 4; depth += 1) {
    const children = [...current.children].filter(isVisibleElement);
    if (children.length === 1 && children[0].children.length > 0) { current = children[0]; continue; }
    const labels = [...new Set(children.map(cleanText).filter(value => validOptionLabel(value, axisName)))];
    if (labels.length >= 1 && labels.length <= 20) return labels;
    if (children.length !== 1) break;
    current = children[0];
  }
  return [];
}

function axisContainerCandidates(header) {
  const result = [];
  const add = element => { if (element && !result.includes(element) && isVisibleElement(element)) result.push(element); };
  add(header.nextElementSibling);
  add(header.parentElement?.nextElementSibling);
  const siblings = [...(header.parentElement?.children || [])];
  const headerIndex = siblings.indexOf(header);
  for (const sibling of siblings.slice(headerIndex + 1, headerIndex + 4)) add(sibling);
  if (header.parentElement) {
    const parentSiblings = [...(header.parentElement.parentElement?.children || [])];
    const parentIndex = parentSiblings.indexOf(header.parentElement);
    for (const sibling of parentSiblings.slice(parentIndex + 1, parentIndex + 3)) add(sibling);
  }
  return result;
}

function semanticSkuGroups() {
  const groups = [];
  const nodes = [...document.querySelectorAll("div,span,label,p,dt")].filter(element => {
    const text = cleanText(element);
    // New 1688 detail pages render price/stock and specification selectors in
    // separate React islands, so requiring the axis header to share one parent
    // with price and stock drops valid groups such as “颜色”.  Exact axis text
    // plus a nearby list of valid options is the safer structural signal.
    return SKU_AXIS_NAMES.has(text) && text.length <= 12 && isVisibleElement(element);
  });
  for (const header of nodes) {
    const name = cleanText(header);
    let best = [];
    for (const container of axisContainerCandidates(header)) {
      const labels = directOptionLabels(container, name);
      if (labels.length) { best = labels; break; }
    }
    if (!best.length) continue;
    const existing = groups.find(group => group.name === name);
    if (!existing || best.length > existing.labels.length) {
      if (existing) existing.labels = best;
      else groups.push({ name, labels: best });
    }
  }
  return groups.filter(group => group.labels.length <= 20).slice(0, 4);
}

function genericSkuGroups() {
  const elements = visibleSkuElements();
  const groups = new Map();
  for (const element of elements) {
    let container = element.parentElement;
    for (let level = 0; container && level < 5; level += 1, container = container.parentElement) {
      const contained = elements.filter(item => container.contains(item));
      if (contained.length >= 2) {
        if (!groups.has(container)) groups.set(container, contained);
        break;
      }
    }
  }
  const unique = [];
  for (const [container, items] of groups.entries()) {
    const labels = [...new Set(items.map(item => cleanText(item.getAttribute("title") || item.getAttribute("aria-label") || item)).filter(Boolean))];
    if (labels.length < 2) continue;
    if (unique.some(group => group.labels.length === labels.length && group.labels.every(label => labels.includes(label)))) continue;
    let name = cleanText(container.previousElementSibling);
    if (!name || name.length > 30) {
      const parentChildren = [...(container.parentElement?.children || [])];
      const index = parentChildren.indexOf(container);
      name = index > 0 ? cleanText(parentChildren[index - 1]) : "";
    }
    unique.push({ name: name && name.length <= 30 ? name : `规格${unique.length + 1}`, labels });
  }
  return unique.filter(group => group.labels.every(label => validOptionLabel(label, group.name))).slice(0, 3);
}

function domSkuGroups() {
  const semantic = semanticSkuGroups();
  return semantic.length ? semantic : genericSkuGroups();
}

function cartesian(groups, limit = 80) {
  let result = [[]];
  for (const group of groups) {
    result = result.flatMap(combination => group.labels.map(label => [...combination, { name: group.name, value: label }])).slice(0, limit);
  }
  return result;
}

function skuSpecificationsMatchGroups(skuList, groups) {
  if (!skuList.length || !groups.length) return false;
  for (const group of groups) {
    const labels = new Set((group.labels || []).map(cleanText).filter(Boolean));
    if (!labels.size) return false;
    const covered = new Set();
    for (const sku of skuList) {
      const matched = (sku.specification_zh || [])
        .map(part => cleanText(part?.value))
        .find(value => labels.has(value));
      if (!matched) return false;
      covered.add(matched);
    }
    if ([...labels].some(label => !covered.has(label))) return false;
  }
  return true;
}

function stableSkuId(parts) {
  const text = parts.map(item => `${item.name}:${item.value}`).join("|");
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) { hash ^= text.charCodeAt(index); hash = Math.imul(hash, 16777619); }
  return `${offerId()}-dom-${(hash >>> 0).toString(16)}`;
}

function currentPriceAndStock() {
  const body = cleanText(document.body?.innerText);
  const direct = [...body.matchAll(/[¥￥]\s*(\d+(?:\.\d+)?)[^¥￥\d]{0,24}库存\s*[:：]?\s*(\d+)/g)];
  if (direct.length) return { price: Number(direct[0][1]), stock: Number(direct[0][2]) };
  const stockMatch = body.match(/库存\s*(\d+)/);
  const selectedAreas = [...document.querySelectorAll("[class*='sku'],[class*='trade'],[class*='order'],[class*='purchase'],[class*='quantity'],div,section")]
    .filter(isVisibleElement)
    .map(element => ({ element, text: cleanText(element) }))
    .filter(item => /[¥￥]\s*\d/.test(item.text) && /库存\s*[:：]?\s*\d+/.test(item.text) && item.text.length < 1200)
    .sort((left, right) => {
      const leftRect = left.element.getBoundingClientRect(); const rightRect = right.element.getBoundingClientRect();
      return leftRect.width * leftRect.height - rightRect.width * rightRect.height || left.text.length - right.text.length;
    });
  const selectedText = selectedAreas[0]?.text || "";
  const priceMatch = selectedText.match(/[¥￥]\s*(\d+(?:\.\d+)?)/);
  const selectedStock = selectedText.match(/库存\s*[:：]?\s*(\d+)/);
  return { price: priceMatch ? Number(priceMatch[1]) : null, stock: selectedStock ? Number(selectedStock[1]) : (stockMatch ? Number(stockMatch[1]) : null) };
}

function smallestVisibleExactText(value) {
  return [...document.querySelectorAll("button,[role='button'],label,li,div,span")]
    .filter(element => isVisibleElement(element) && cleanText(element) === value)
    .sort((left, right) => {
      const leftRect = left.getBoundingClientRect(); const rightRect = right.getBoundingClientRect();
      return leftRect.width * leftRect.height - rightRect.width * rightRect.height;
    })[0] || null;
}

function imageElementUrl(image) {
  if (!image) return "";
  const srcset = cleanText(image.getAttribute?.("srcset") || image.getAttribute?.("data-srcset")).split(",")[0]?.trim().split(/\s+/)[0];
  const attributes = [
    "data-src", "data-lazy-src", "data-lazyload", "data-lazy-img",
    "data-lazy-image", "data-original", "data-image", "data-img",
  ];
  const lazy = attributes.map(name => image.getAttribute?.(name)).find(Boolean);
  return absoluteUrl(image.currentSrc || image.src || lazy || srcset);
}

function optionImage(element, value) {
  const candidates = [];
  const add = candidate => { if (candidate && !candidates.includes(candidate)) candidates.push(candidate); };
  add(element);
  add(element?.closest?.("button,label,li,[role='button'],[role='option'],[class*='item'],[class*='Item']"));
  let current = element?.parentElement;
  for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) add(current);
  for (const candidate of candidates) {
    const ownLabel = cleanText(candidate.getAttribute?.("title") || candidate.getAttribute?.("aria-label"));
    const candidateText = cleanText(candidate);
    const matches = ownLabel === value || candidateText === value || (
      candidateText.includes(value) && candidateText.length <= value.length + 12
    );
    if (!matches) continue;
    const images = candidate.matches?.("img") ? [candidate] : [...(candidate.querySelectorAll?.("img") || [])];
    for (const image of images) {
      const url = imageElementUrl(image);
      if (url) return highResolutionImageUrl(url);
    }
    for (const background of [candidate, ...(candidate.querySelectorAll?.("[style*='background']") || [])]) {
      const match = getComputedStyle(background).backgroundImage.match(/url\(["']?([^"')]+)["']?\)/i);
      const backgroundUrl = absoluteUrl(match?.[1]);
      if (backgroundUrl) return highResolutionImageUrl(backgroundUrl);
    }
  }
  return "";
}

function imageIdentity(value) {
  try {
    const parsed = new URL(value, location.href);
    return `${parsed.hostname}${parsed.pathname}`.replace(/_(?:\d+x\d+|q\d+|\.webp).*$/i, "");
  } catch (_error) {
    return cleanText(value).split(/[?#]/)[0];
  }
}

function axisValueImage(axisValues, value) {
  return highResolutionImageUrl(axisValues?.get?.(cleanText(value))?.image || "");
}

function visibleSkuOptionImages(groups = domSkuGroups(), axisValues = new Map()) {
  const bySpec = new Map();
  for (const group of groups) {
    for (const value of group.labels || []) {
      const element = smallestVisibleExactText(value);
      const url = optionImage(element, value) || axisValueImage(axisValues, value);
      if (!url) continue;
      bySpec.set(`${group.name}\u0000${value}`, url);
      if (!bySpec.has(value)) bySpec.set(value, url);
    }
  }
  return bySpec;
}

function currentGalleryImage() {
  const preferred = [...document.querySelectorAll("[class*='gallery'] img,[class*='Gallery'] img,[class*='main-image'] img,[class*='mainImage'] img,[class*='preview'] img,[class*='Preview'] img,[class*='detail-gallery'] img")];
  const fallback = [...document.images].filter(image => image.getBoundingClientRect().top < window.innerHeight * 1.4);
  const ranked = [...new Set([...preferred, ...fallback])].map(image => {
    const rect = image.getBoundingClientRect();
    const url = highResolutionImageUrl(image.currentSrc || image.src || image.dataset.src || image.dataset.lazyload);
    const area = Math.max(image.naturalWidth * image.naturalHeight, rect.width * rect.height);
    const preferredBonus = preferred.includes(image) ? 10000000 : 0;
    return { url, score: preferredBonus + area, visible: rect.width >= 120 && rect.height >= 120 };
  }).filter(item => item.url && item.visible && /alicdn|1688|alibaba/i.test(item.url));
  ranked.sort((left, right) => right.score - left.score);
  return ranked[0]?.url || "";
}

async function enrichSkuImages(skuList, mainImage, groups = domSkuGroups(), axisValues = new Map()) {
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const visibleImages = visibleSkuOptionImages(groups, axisValues);
  let captured = 0;
  let overridden = 0;
  for (const sku of skuList) {
    let optionUrl = "";
    for (const part of sku.specification_zh || []) {
      optionUrl = optionUrl || visibleImages.get(`${part.name}\u0000${part.value}`) || visibleImages.get(part.value) || axisValueImage(axisValues, part.value) || "";
      const element = smallestVisibleExactText(part.value);
      if (!element) continue;
      element.click();
      optionUrl = optionUrl || optionImage(element, part.value);
      await delay(360);
    }
    if (optionUrl) await delay(120);
    const dynamicUrl = currentGalleryImage();
    const previous = sku.image_url || "";
    const resolved = highResolutionImageUrl(optionUrl || dynamicUrl || previous || mainImage || "");
    if (resolved) {
      sku.image_url = resolved;
      if (resolved !== mainImage) captured += 1;
      if (previous && imageIdentity(previous) !== imageIdentity(resolved)) overridden += 1;
    }
  }
  return {
    captured,
    overridden,
    visibleOptionImageCount: new Set([...visibleImages.values()].map(imageIdentity).filter(Boolean)).size,
    uniqueSkuImageCount: new Set(skuList.map(sku => imageIdentity(sku.image_url)).filter(Boolean)).size,
  };
}

async function enrichSkuCommerce(skuList, priceRanges) {
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  let filledPrices = 0;
  let filledStocks = 0;
  for (const sku of skuList) {
    if (sku.purchase_price_cny !== null && sku.purchase_price_cny !== undefined && sku.stock !== null && sku.stock !== undefined) continue;
    let clicked = false;
    for (const part of sku.specification_zh || []) {
      const element = smallestVisibleExactText(part.value);
      if (!element) continue;
      element.click(); clicked = true;
      await delay(320);
    }
    if (clicked) await delay(180);
    const current = currentPriceAndStock();
    if ((sku.purchase_price_cny === null || sku.purchase_price_cny === undefined) && current.price !== null && current.price > 0) {
      sku.purchase_price_cny = current.price; filledPrices += 1;
    }
    if ((sku.stock === null || sku.stock === undefined) && current.stock !== null) {
      sku.stock = current.stock; filledStocks += 1;
    }
  }
  if (skuList.length === 1 && (skuList[0].purchase_price_cny === null || skuList[0].purchase_price_cny === undefined)) {
    const fallbackPrice = priceRanges.find(item => Number(item.price_cny) > 0)?.price_cny;
    if (fallbackPrice !== undefined) { skuList[0].purchase_price_cny = fallbackPrice; filledPrices += 1; }
  }
  return { filledPrices, filledStocks };
}

async function collectDomSkus(mainImage, groups = domSkuGroups(), axisValues = new Map()) {
  if (!groups.length) return [];
  const combinations = cartesian(groups);
  const result = [];
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  for (const combination of combinations) {
    let valid = true;
    let image = "";
    for (const part of combination) {
      if ((groups.find(group => group.name === part.name)?.labels.length || 0) === 1) continue;
      const element = smallestVisibleExactText(part.value);
      if (!element) { valid = false; break; }
      element.click();
      image = image || optionImage(element, part.value) || axisValueImage(axisValues, part.value);
      await delay(260);
    }
    if (!valid) continue;
    const current = currentPriceAndStock();
    if (current.price === null || current.price <= 0) continue;
    const dynamicImage = currentGalleryImage();
    result.push({ sku_id: stableSkuId(combination), specification_zh: combination, specification_text_zh: combination.map(item => `${item.name}:${item.value}`).join("; "), purchase_price_cny: current.price, stock: current.stock, image_url: highResolutionImageUrl(image || dynamicImage || mainImage) || null });
  }
  return result;
}

async function scan() {
  const mainData = await requestMainCandidates(); const scriptRoots = parsedJsonRoots(); const roots = [...mainData.candidates, ...scriptRoots];
  if (!roots.length) throw new Error("没有捕获到1688商品接口数据。请刷新当前详情页，等待价格和规格显示后重新采集");
  const axisValues = collectAxisValues(roots); let skuList = collectStructuredSkus(roots, axisValues); const images = collectImages(roots); const shipping = collectShipping(roots); const priceRanges = collectPriceRanges(roots); const warnings = []; const pageGroups = domSkuGroups(); const visibleOptions = Math.max(visibleSkuOptionCount(), pageGroups.reduce((total, group) => total + group.labels.length, 0)); const expectedSkuCount = pageGroups.length ? pageGroups.reduce((total, group) => total * group.labels.length, 1) : null;
  const structuredSpecificationsValid = skuSpecificationsMatchGroups(skuList, pageGroups);
  const mustRebuildFromPage = expectedSkuCount && (
    skuList.length !== expectedSkuCount || !structuredSpecificationsValid || pageGroups.length === 1
  );
  if (mustRebuildFromPage) {
    const pageSkuList = await collectDomSkus(images.main[0]?.url, pageGroups, axisValues);
    if (pageSkuList.length) {
      skuList = pageSkuList;
      warnings.push(pageGroups.length === 1
        ? "单规格SKU已按页面每个可见选项逐项重建并绑定缩略图"
        : "完整SKU已按页面规格标题和选项逐项校验");
    }
  }
  if (!skuList.length && visibleOptions > 1) { const fallbackGroups = domSkuGroups(); skuList = await collectDomSkus(images.main[0]?.url, fallbackGroups, axisValues); if (skuList.length) warnings.push("完整SKU由页面规格逐项校验生成"); }
  if (expectedSkuCount && skuList.length !== expectedSkuCount) throw new Error(`页面规格应有 ${expectedSkuCount} 个SKU，但实际只校验到 ${skuList.length} 个，已阻止发送`);
  if (!expectedSkuCount && skuList.length > 30) throw new Error(`页面没有确认到有效规格组，却生成了 ${skuList.length} 个SKU，已阻止发送，请更新采集规则`);
  if (!skuList.length && visibleOptions <= 1) { const priceText = cleanText(document.querySelector("[class*='price']")); skuList = [{ sku_id: `${offerId()}-default`, specification_zh: [], specification_text_zh: "默认规格", purchase_price_cny: priceRanges[0]?.price_cny ?? numberValue(priceText), stock: null, image_url: images.main[0]?.url || null }]; }
  if (!skuList.length || (skuList.length === 1 && visibleOptions > 1)) throw new Error(`页面显示多个规格，但只读取到 ${skuList.length} 个SKU。请刷新详情页，等待规格和价格加载完成后重新采集`);
  if (skuList.length > 1 && skuList.every(sku => !(sku.specification_zh || []).length)) throw new Error("读取到了多个SKU编号，但没有绑定页面规格名称和规格图片。已阻止复用同一张主图，请更新插件并刷新详情页后重试");
  const commerceEnrichment = await enrichSkuCommerce(skuList, priceRanges);
  const imageEnrichment = await enrichSkuImages(skuList, images.main[0]?.url || "", pageGroups, axisValues);
  if (imageEnrichment.visibleOptionImageCount > 1 && imageEnrichment.uniqueSkuImageCount < Math.min(imageEnrichment.visibleOptionImageCount, skuList.length)) {
    throw new Error(`页面有 ${imageEnrichment.visibleOptionImageCount} 张不同SKU规格图，但只绑定到 ${imageEnrichment.uniqueSkuImageCount} 张。已阻止发送错误图片，请刷新详情页后重试`);
  }
  const missingPrices = skuList.filter(sku => sku.purchase_price_cny === null || sku.purchase_price_cny === undefined).length;
  if (missingPrices) warnings.push(`${missingPrices}个SKU没有读取到采购价，发送前请刷新页面重试`);
  if (missingPrices === skuList.length) throw new Error("没有读取到任何SKU采购价。请刷新1688详情页，等待价格加载完成后重试");
  const title = collectTitle(roots); if (!title) throw new Error("没有识别到商品标题，请等待页面加载完成后重试"); if (!images.main.length) throw new Error("没有识别到商品主图，请等待页面加载完成后重试");
  const attributes = collectAttributes(roots); let categoryId = ""; let supplierName = "";
  walk(roots, object => { if (!categoryId) { const value = cleanText(firstValue(object, ["thirdCategoryId", "categoryId", "categoryID", "leafCategoryId"])); if (/^\d+$/.test(value)) categoryId = value; } if (!supplierName) supplierName = cleanText(firstValue(object, ["companyName", "supplierName", "shopName"])); });
  const breadcrumbs = [...document.querySelectorAll("[class*='breadcrumb'] a,[class*='crumb'] a")].map(cleanText).filter(Boolean).slice(0, 20);
  return { source_offer_id: offerId(), product_url: location.href, title_zh: title, images: images.main, detail_images: images.detail, sku_list: skuList, price_ranges: priceRanges, attributes_zh: attributes, category_id: categoryId, category_path_zh: breadcrumbs, supplier_info: { name: supplierName || null, min_order: priceRanges.find(item => item.minimum_quantity !== null)?.minimum_quantity ?? null, ship_from: shipping.ship_from_zh }, shipping_info: shipping, capture_diagnostics: { collector_version: "0.3.5", main_candidate_count: mainData.candidates.length, intercepted_response_count: mainData.response_urls.length, visible_sku_option_count: visibleOptions, expected_sku_count: expectedSkuCount, structured_specifications_valid: structuredSpecificationsValid, enriched_sku_price_count: commerceEnrichment.filledPrices, enriched_sku_stock_count: commerceEnrichment.filledStocks, enriched_sku_image_count: imageEnrichment.captured, overridden_sku_image_count: imageEnrichment.overridden, visible_option_image_count: imageEnrichment.visibleOptionImageCount, sku_image_count: skuList.filter(sku => Boolean(sku.image_url)).length, unique_sku_image_count: imageEnrichment.uniqueSkuImageCount, spec_groups: pageGroups.map(group => ({ name: group.name, count: group.labels.length })), warnings }, captured_at: new Date().toISOString() };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NORTHSTAR_1688_SCAN") return false;
  scan().then(product => sendResponse({ ok: true, product })).catch(error => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});

function installFloatingCollector() {
  if (!offerId() || document.querySelector(".ns1688-launch")) return;
  let capturedProduct = null;
  const launch = document.createElement("button");
  launch.type = "button";
  launch.className = "ns1688-launch";
  launch.title = "采集当前1688商品";
  launch.textContent = "采";
  const panel = document.createElement("aside");
  panel.className = "ns1688-panel ns1688-hidden";
  const head = document.createElement("div");
  head.className = "ns1688-head";
  const heading = document.createElement("b"); heading.textContent = "Northstar · 1688商品采集";
  const close = document.createElement("button"); close.type = "button"; close.textContent = "×";
  head.append(heading, close);
  const body = document.createElement("div"); body.className = "ns1688-body";
  const intro = document.createElement("div"); intro.className = "ns1688-summary"; intro.textContent = "直接读取当前商品、完整SKU、采购价、库存、图片和运费，并发送至固定ERP。";
  const scanButton = document.createElement("button"); scanButton.type = "button"; scanButton.className = "ns1688-action"; scanButton.textContent = "读取当前商品 →";
  const productBox = document.createElement("div"); productBox.className = "ns1688-info ns1688-hidden";
  const toolbar = document.createElement("div"); toolbar.className = "ns1688-toolbar ns1688-hidden";
  const selectLabel = document.createElement("label");
  const selectAll = document.createElement("input"); selectAll.type = "checkbox"; selectAll.checked = true;
  const selectText = document.createElement("span"); selectText.textContent = "全选SKU";
  const count = document.createElement("strong");
  selectLabel.append(selectAll, selectText); toolbar.append(selectLabel, count);
  const skuList = document.createElement("div"); skuList.className = "ns1688-skus";
  const sendButton = document.createElement("button"); sendButton.type = "button"; sendButton.className = "ns1688-action ns1688-hidden"; sendButton.textContent = "发送到ERP第一阶段 →";
  const status = document.createElement("div"); status.className = "ns1688-status"; status.textContent = "ERP已固定为 https://8.217.125.205，无需填写网址。";
  body.append(intro, scanButton, productBox, toolbar, skuList, sendButton, status);
  panel.append(head, body); document.body.append(launch, panel);

  function setCollectorStatus(message, error = false) {
    status.textContent = message; status.dataset.error = error ? "1" : "0";
  }

  function selectedInputs() { return [...skuList.querySelectorAll("input[type=checkbox]")]; }

  function updateSelection() {
    const inputs = selectedInputs(); const selected = inputs.filter(input => input.checked).length;
    count.textContent = `保留 ${selected}/${inputs.length}`;
    selectAll.checked = Boolean(inputs.length) && selected === inputs.length;
    selectAll.indeterminate = selected > 0 && selected < inputs.length;
    sendButton.disabled = selected === 0;
  }

  function renderCaptured(product) {
    productBox.replaceChildren(); productBox.classList.remove("ns1688-hidden");
    const image = document.createElement("img"); image.src = product.images?.[0]?.url || "";
    const details = document.createElement("div"); const title = document.createElement("b"); title.textContent = product.title_zh || "未识别标题";
    const meta = document.createElement("small"); meta.textContent = `商品ID ${product.source_offer_id} · ${product.sku_list?.length || 0}个SKU · 运费 ${product.shipping_info?.freight_cny ?? "未读取"}`;
    details.append(title, meta); productBox.append(image, details);
    toolbar.classList.remove("ns1688-hidden"); sendButton.classList.remove("ns1688-hidden"); skuList.replaceChildren();
    (product.sku_list || []).forEach((sku, index) => {
      const row = document.createElement("label"); row.className = "ns1688-sku";
      const input = document.createElement("input"); input.type = "checkbox"; input.checked = true; input.dataset.index = String(index); input.onchange = updateSelection;
      const skuImage = document.createElement("img"); skuImage.src = sku.image_url || product.images?.[0]?.url || "";
      const text = document.createElement("span"); const name = document.createElement("b"); name.textContent = sku.specification_text_zh || "默认规格";
      const info = document.createElement("small"); info.textContent = `¥${sku.purchase_price_cny ?? "未读取"} · 库存 ${sku.stock ?? "未知"}`;
      text.append(name, info); row.append(input, skuImage, text); skuList.append(row);
    });
    updateSelection();
  }

  launch.onclick = () => panel.classList.toggle("ns1688-hidden");
  close.onclick = () => panel.classList.add("ns1688-hidden");
  selectAll.onchange = () => { selectedInputs().forEach(input => { input.checked = selectAll.checked; }); updateSelection(); };
  scanButton.onclick = async () => {
    scanButton.disabled = true; setCollectorStatus("正在读取SKU、采购价、库存与运费…");
    try { capturedProduct = await scan(); renderCaptured(capturedProduct); setCollectorStatus("采集完成，请取消不想上架的SKU后发送。" ); }
    catch (error) { setCollectorStatus(error.message || String(error), true); }
    finally { scanButton.disabled = false; }
  };
  sendButton.onclick = async () => {
    if (!capturedProduct) return;
    const selected = selectedInputs().filter(input => input.checked).map(input => capturedProduct.sku_list[Number(input.dataset.index)]);
    if (!selected.length) { setCollectorStatus("至少保留一个SKU", true); return; }
    sendButton.disabled = true; setCollectorStatus("正在发送到ERP第一阶段…");
    const product = { ...capturedProduct, sku_list: selected, capture_diagnostics: { ...(capturedProduct.capture_diagnostics || {}), captured_sku_count: capturedProduct.sku_list.length, selected_sku_count: selected.length } };
    try {
      const response = await chrome.runtime.sendMessage({ type: "NORTHSTAR_1688_SEND", product });
      if (!response?.ok) throw new Error(response?.error || "发送失败");
      setCollectorStatus(`发送成功：商品 ${response.data.pipeline_id}，保留 ${response.data.sku_count} 个SKU。`);
    } catch (error) { setCollectorStatus(error.message || String(error), true); }
    finally { sendButton.disabled = false; }
  };
}

installFloatingCollector();
