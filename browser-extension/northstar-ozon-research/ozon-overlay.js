(() => {
  if (window.__northstarOzonOverlayInstalled) return;
  window.__northstarOzonOverlayInstalled = true;

  const PRODUCT_PATH = /\/product\/(?:[^/?#]+-)?(\d{5,})(?:\/|[?#]|$)/i;
  const LABELS = /^(?:Бренд|Продавец|Доставка|Получение|Вес(?: товара)?|Габариты|Размеры|Страна-изготовитель|Цвет|Артикул|Код товара)\s*[:：]?\s*$/i;
  let scheduled = null;
  let currentDetailId = "";
  const publicCache = new Map();
  const sellerCache = new Map();

  function clean(value) {
    return String(value ?? "").replace(/[\u00a0\u2009\u202f]/g, " ").replace(/\s+/g, " ").trim();
  }

  function productId(value) {
    try { return new URL(value, location.origin).pathname.match(PRODUCT_PATH)?.[1] || ""; }
    catch (_) { return String(value || "").match(PRODUCT_PATH)?.[1] || ""; }
  }

  function pageLines() {
    const main = document.querySelector("main") || document.body;
    return String(main?.innerText || "").split(/\n+/).map(clean).filter(Boolean).slice(0, 10000);
  }

  function labeledValue(lines, names) {
    const wanted = names.map(name => name.toLowerCase());
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const split = line.match(/^([^:：]{1,45})\s*[:：]\s*(.+)$/);
      if (split && wanted.includes(clean(split[1]).toLowerCase())) return clean(split[2]);
      if (wanted.includes(line.replace(/[:：]$/, "").toLowerCase())) {
        const next = lines[index + 1] || "";
        if (next && !LABELS.test(next) && next.length < 180) return next;
      }
    }
    return "";
  }

  function rubles(value) {
    const match = clean(value).match(/([\d ]{1,15})\s*₽/);
    if (!match) return null;
    const amount = Number(match[1].replace(/\s/g, ""));
    return Number.isFinite(amount) && amount > 0 ? amount : null;
  }

  function formatRub(value) {
    return Number.isFinite(value) ? `${new Intl.NumberFormat("ru-RU").format(value)} ₽` : "未公开";
  }

  function formatNumber(value, suffix = "") {
    if (value === null || value === undefined || value === "") return "未公开";
    const number = Number(value);
    return Number.isFinite(number) ? `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(number)}${suffix}` : "未公开";
  }

  function percentage(current, original) {
    if (!current || !original || original <= current) return "—";
    return `-${Math.round((1 - current / original) * 100)}%`;
  }

  function firstMatch(text, patterns) {
    for (const pattern of patterns) {
      const match = String(text || "").match(pattern);
      if (match?.[1]) return clean(match[1]);
    }
    return "";
  }

  function addRow(container, label, value, tone = "") {
    const row = document.createElement("div");
    row.className = "ns-ozon-row";
    const key = document.createElement("span");
    key.textContent = `${label}：`;
    const result = document.createElement("strong");
    result.textContent = value || "未公开";
    if (tone) result.dataset.tone = tone;
    row.append(key, result);
    container.append(row);
  }

  function schemaProduct() {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(script.textContent || "null");
        const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
        while (queue.length) {
          const item = queue.shift();
          if (!item || typeof item !== "object") continue;
          const type = Array.isArray(item["@type"]) ? item["@type"] : [item["@type"]];
          if (type.some(value => String(value).toLowerCase() === "product")) return item;
          if (Array.isArray(item["@graph"])) queue.push(...item["@graph"]);
        }
      } catch (_) {}
    }
    return null;
  }

  function breadcrumbCategory(schema, publicData, brand) {
    const selectors = '[data-widget="breadCrumbs"] a[href], nav[aria-label*="breadcrumb" i] a[href]';
    const values = [...document.querySelectorAll(selectors)].map(node => clean(node.textContent)).filter(value => value && value.length < 80 && !/главная|ozon|китай|россия/i.test(value));
    if (values.length) return values.at(-1);
    if (publicData?.breadcrumbs?.length) return publicData.breadcrumbs.at(-1);
    const schemaCategory = clean(schema?.category);
    if (schemaCategory && schemaCategory.toLowerCase() !== clean(brand).toLowerCase()) return schemaCategory;
    return "未公开";
  }

  function decodeWidgetStates(payload) {
    const result = [];
    const states = payload?.widgetStates;
    if (!states || typeof states !== "object") return result;
    for (const [key, raw] of Object.entries(states)) {
      let value = raw;
      if (typeof value === "string" && /^[\[{]/.test(value.trim())) {
        try { value = JSON.parse(value); } catch (_) {}
      }
      result.push({ key, value });
    }
    return result;
  }

  function walk(value, visit, depth = 0) {
    if (depth > 9 || value === null || value === undefined) return;
    if (typeof value === "object") {
      visit(value);
      for (const child of Object.values(value)) walk(child, visit, depth + 1);
    }
  }

  function aspectPairs(states) {
    const pairs = new Map();
    for (const { value } of states) walk(value, object => {
      const name = clean(object.name || object.title || object.attributeName || object.label);
      let raw = object.value ?? object.text ?? object.values ?? object.attributeValue;
      if (Array.isArray(raw)) raw = raw.map(item => clean(typeof item === "object" ? (item.value || item.text || item.name) : item)).filter(Boolean).join(", ");
      if (raw && typeof raw === "object") raw = raw.value || raw.text || raw.name;
      const result = clean(raw);
      if (name && result && name.length < 90 && result.length < 240 && !pairs.has(name.toLowerCase())) pairs.set(name.toLowerCase(), result);
    });
    return pairs;
  }

  function mergePairs(target, source) {
    for (const [name, value] of source) if (!target.has(name)) target.set(name, value);
    return target;
  }

  function breadcrumbValues(states) {
    const values = [];
    for (const { key, value } of states) {
      if (!/bread/i.test(key)) continue;
      walk(value, object => {
        const candidate = clean(object.title || object.text || object.name);
        if (candidate && candidate.length < 80 && !/главная|ozon|китай|россия/i.test(candidate)) values.push(candidate);
      });
    }
    return values.filter((value, index) => values.indexOf(value) === index);
  }

  function aspect(pairs, patterns) {
    for (const [name, value] of pairs) if (patterns.some(pattern => pattern.test(name))) return value;
    return "";
  }

  async function publicProductData(id) {
    if (publicCache.has(id)) return publicCache.get(id);
    const promise = (async () => {
      const data = { aspects: new Map(), breadcrumbs: [], sellers: [], publicError: "" };
      try {
        const endpoint = `/api/entrypoint-api.bx/page/json/v2?url=${encodeURIComponent(location.pathname + location.search)}`;
        const response = await fetch(endpoint, { credentials: "include", cache: "no-store", headers: { accept: "application/json" } });
        if (!response.ok) throw new Error(`商品接口 HTTP ${response.status}`);
        const states = decodeWidgetStates(await response.json());
        data.aspects = aspectPairs(states);
        data.breadcrumbs = breadcrumbValues(states);
      } catch (error) { data.publicError = error.message || String(error); }
      try {
        const modal = `/modal/aspectsNew?product_id=${encodeURIComponent(id)}`;
        const response = await fetch(`/api/entrypoint-api.bx/page/json/v2?url=${encodeURIComponent(modal)}`, { credentials: "include", cache: "no-store", headers: { accept: "application/json" } });
        if (response.ok) mergePairs(data.aspects, aspectPairs(decodeWidgetStates(await response.json())));
      } catch (_) {}
      try {
        const modal = `/modal/otherOffersFromSellers?product_id=${encodeURIComponent(id)}`;
        const response = await fetch(`/api/entrypoint-api.bx/page/json/v2?url=${encodeURIComponent(modal)}`, { credentials: "include", cache: "no-store", headers: { accept: "application/json" } });
        if (response.ok) {
          for (const { value } of decodeWidgetStates(await response.json())) walk(value, object => {
            if (!Array.isArray(object.sellers)) return;
            for (const seller of object.sellers) {
              const price = rubles(seller.price || seller.priceText || seller.cardPrice || "");
              const name = clean(seller.name || seller.sellerName || seller.title);
              if (price || name) data.sellers.push({ price, name });
            }
          });
        }
      } catch (_) {}
      data.sellers = data.sellers.filter((item, index, array) => index === array.findIndex(other => other.price === item.price && other.name === item.name));
      return data;
    })();
    publicCache.set(id, promise);
    return promise;
  }

  async function sellerMetrics(id, force = false) {
    if (force) sellerCache.delete(id);
    if (sellerCache.has(id)) return sellerCache.get(id);
    const promise = new Promise(resolve => {
      try {
        chrome.runtime.sendMessage({ type: "NORTHSTAR_OZON_METRICS", sku: id, force }, response => {
          if (chrome.runtime.lastError) resolve({ ok: false, error: "请打开并登录 Ozon Seller 后台" });
          else resolve(response || { ok: false, error: "卖家数据无响应" });
        });
      } catch (_) { resolve({ ok: false, error: "插件后台未连接" }); }
    });
    sellerCache.set(id, promise);
    return promise;
  }

  function metric(source, ...keys) {
    for (const key of keys) if (source?.[key] !== null && source?.[key] !== undefined && source?.[key] !== "") return source[key];
    return null;
  }

  function sellerDimensions(source) {
    const direct = clean(metric(source, "packagingDimensions", "packaging_dimensions", "custom_volume"));
    if (direct) return direct;
    const dimensions = source?.dimensions;
    if (!dimensions || typeof dimensions !== "object") return "";
    const values = [dimensions.length || dimensions.depth, dimensions.width, dimensions.height].map(value => Number(value));
    return values.every(value => Number.isFinite(value) && value > 0) ? values.join(" × ") : "";
  }

  function sellerWeight(source) {
    const value = metric(source, "packagingWeight", "packaging_weight", "custom_weight", "weight");
    return value === null ? "" : clean(value);
  }

  async function detailData(id, force = false) {
    const schema = schemaProduct() || {};
    const lines = pageLines();
    const flat = lines.join(" \n ");
    const offer = Array.isArray(schema.offers) ? schema.offers[0] : (schema.offers || {});
    const rating = schema.aggregateRating || {};
    const prices = [...flat.matchAll(/([\d\s\u00a0\u2009\u202f]+)\s*₽/g)].map(match => Number(match[1].replace(/\s/g, ""))).filter(value => Number.isFinite(value) && value > 0);
    const price = Number(offer.price) || prices[0] || null;
    const original = prices.find(value => price && value > price) || null;
    const publicData = await publicProductData(id);
    const seller = await sellerMetrics(id, force);
    const pairs = publicData.aspects;
    const sellerData = seller?.data || {};
    const brandValue = typeof schema.brand === "object" ? schema.brand?.name : schema.brand;
    const brand = clean(metric(sellerData, "brand", "brand_name")) || clean(brandValue) || aspect(pairs, [/^бренд$/, /brand/]) || labeledValue(lines, ["Бренд"]);
    const sellerCategory = clean(metric(sellerData, "category3", "category_name", "category", "category2", "category1"));
    const sellerPrices = publicData.sellers.map(item => item.price).filter(Number.isFinite);
    return {
      id,
      title: clean(schema.name) || clean(document.querySelector("h1")?.textContent) || clean(document.title),
      category: sellerCategory || breadcrumbCategory(schema, publicData, brand),
      brand,
      price, original, discount: percentage(price, original),
      rating: clean(rating.ratingValue) || firstMatch(flat, [/(?:Рейтинг|评分)\s*[:：]?\s*(\d(?:[.,]\d)?)/i]),
      reviews: clean(rating.reviewCount || rating.ratingCount) || firstMatch(flat, [/(\d[\d\s]*)\s*(?:отзыв|оценк)/i]),
      sold: firstMatch(flat, [/(\d[\d\s+.,]*(?:тыс\.?)?)\s*(?:купили|продано|заказали)/i]),
      sellerName: labeledValue(lines, ["Продавец"]),
      delivery: labeledValue(lines, ["Доставка", "Получение"]),
      dimensions: aspect(pairs, [/габарит/, /размер.*упаков/, /длина.*ширина.*высота/]) || sellerDimensions(sellerData) || labeledValue(lines, ["Габариты", "Размеры"]),
      weight: aspect(pairs, [/^вес/, /масса/]) || sellerWeight(sellerData) || labeledValue(lines, ["Вес товара", "Вес"]),
      country: aspect(pairs, [/страна.*изготов/, /страна.*производ/]) || labeledValue(lines, ["Страна-изготовитель"]),
      sellerMetrics: sellerData,
      sellerStatus: seller?.ok ? "已连接 Ozon Seller" : (seller?.error || "未授权"),
      followCount: publicData.sellers.length,
      followMin: sellerPrices.length ? Math.min(...sellerPrices) : null,
      followMax: sellerPrices.length ? Math.max(...sellerPrices) : null,
    };
  }

  async function detailPanel(id, force = false) {
    document.querySelectorAll(".ns-ozon-detail").forEach(node => node.remove());
    const panel = document.createElement("aside");
    panel.className = "ns-ozon-analysis ns-ozon-detail";
    panel.dataset.productId = id;
    panel.innerHTML = '<header><b>NORTHSTAR · OZON DATA</b><div><button type="button" title="刷新">↻</button><button type="button" title="收起/展开">−</button></div></header><div class="ns-ozon-loading">正在读取公开数据与 Ozon Seller 指标…</div>';
    document.body.append(panel);
    const [refresh, toggle] = panel.querySelectorAll("button");
    refresh.onclick = () => { publicCache.delete(id); detailPanel(id, true); };
    toggle.onclick = () => { const collapsed = panel.classList.toggle("collapsed"); toggle.textContent = collapsed ? "+" : "−"; };
    const data = await detailData(id, force);
    if (!panel.isConnected) return;
    panel.querySelector(".ns-ozon-loading")?.remove();
    const heading = document.createElement("div"); heading.className = "ns-ozon-title"; heading.textContent = data.title;
    const body = document.createElement("section");
    addRow(body, "类目", data.category);
    addRow(body, "SKU", data.id, "blue");
    addRow(body, "品牌", data.brand || "未公开", "blue");
    addRow(body, "当前售价", formatRub(data.price), "blue");
    addRow(body, "原价 / 折扣", `${formatRub(data.original)} / ${data.discount}`, data.discount !== "—" ? "red" : "");
    addRow(body, "评分 / 评论", [data.rating, data.reviews].filter(Boolean).join(" / ") || "未公开");
    addRow(body, "页面可见销量", data.sold || "未公开");
    addRow(body, "卖家 / 配送", [data.sellerName, data.delivery].filter(Boolean).join(" / ") || "未公开");
    addRow(body, "长×宽×高", data.dimensions || "未公开");
    addRow(body, "重量 / 原产国", [data.weight, data.country].filter(Boolean).join(" / ") || "未公开");
    addRow(body, "月销量", formatNumber(metric(data.sellerMetrics, "soldCount", "sold_count")), "blue");
    addRow(body, "月销售额", formatNumber(metric(data.sellerMetrics, "soldSum", "gmvSum", "revenue"), " ₽"), "blue");
    addRow(body, "月周转动态", formatNumber(metric(data.sellerMetrics, "salesDynamics", "sales_dynamics"), "%"));
    addRow(body, "日销量", metric(data.sellerMetrics, "soldCount", "sold_count") != null ? formatNumber(Number(metric(data.sellerMetrics, "soldCount", "sold_count")) / 30) : "未公开");
    addRow(body, "广告费占比", formatNumber(metric(data.sellerMetrics, "drr"), "%"));
    addRow(body, "促销天数 / 折扣", `${formatNumber(metric(data.sellerMetrics, "daysInPromo", "days_in_promo"), " 天")} / ${formatNumber(metric(data.sellerMetrics, "discount"), "%")}`);
    addRow(body, "付费推广天数", formatNumber(metric(data.sellerMetrics, "daysWithTrafarets", "days_with_trafarets"), " 天"));
    addRow(body, "商品卡浏览量", formatNumber(metric(data.sellerMetrics, "qtyViewPdp", "qty_view_pdp")));
    addRow(body, "搜索目录浏览量", formatNumber(metric(data.sellerMetrics, "sessionCountSearch", "session_count_search")));
    addRow(body, "展示转化率", formatNumber(metric(data.sellerMetrics, "convViewToOrder"), "%"));
    addRow(body, "退货取消率", formatNumber(metric(data.sellerMetrics, "nullableRedemptionRate", "redemption_rate"), "%"));
    addRow(body, "发货模式", metric(data.sellerMetrics, "salesSchema", "sales_schema") || "未公开");
    addRow(body, "类目佣金", formatNumber(metric(data.sellerMetrics, "commission_rate", "commissionRate"), "%"));
    addRow(body, "上架时间", metric(data.sellerMetrics, "nullableCreateDate", "created_at", "visible_since") || "未公开");
    addRow(body, "跟卖卖家数", data.followCount ? `${data.followCount} 家` : "未公开");
    addRow(body, "跟卖最低 / 最高", data.followMin ? `${formatRub(data.followMin)} / ${formatRub(data.followMax)}` : "未公开");
    addRow(body, "Seller 数据源", data.sellerStatus, data.sellerStatus.startsWith("已连接") ? "blue" : "muted");
    const note = document.createElement("footer");
    note.textContent = "公开字段来自当前 Ozon 页面；经营指标仅在浏览器已登录 Ozon Seller 时读取，不使用虚构估算值。";
    panel.append(heading, body, note);
  }

  function cardRoot(anchor) {
    const preferred = anchor.closest('[data-index], [data-widget*="tile" i], article');
    if (preferred && preferred.querySelector("img")) return preferred;
    let current = anchor.parentElement;
    for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
      const rect = current.getBoundingClientRect();
      if (rect.width >= 170 && rect.width <= 720 && rect.height >= 220 && current.querySelector("img")) return current;
    }
    return null;
  }

  function cardData(root, anchor, id) {
    const text = clean(root.innerText);
    const prices = [...text.matchAll(/([\d\s\u00a0\u2009\u202f]+)\s*₽/g)].map(match => Number(match[1].replace(/\s/g, ""))).filter(value => Number.isFinite(value) && value > 0);
    const price = prices[0] || null;
    const original = prices.find(value => price && value > price) || null;
    return { id, price, original, rating: firstMatch(text, [/(\d(?:[.,]\d))\s*(?:★|⭐|·|\s)/]), reviews: firstMatch(text, [/(\d[\d\s]*)\s*(?:отзыв|оценк)/i]), sold: firstMatch(text, [/(\d[\d\s+.,]*(?:тыс\.?)?)\s*(?:купили|продано|заказали)/i]) };
  }

  function addCardPanel(root, anchor, id) {
    if (root.querySelector(`.ns-ozon-card[data-product-id="${CSS.escape(id)}"]`)) return;
    const data = cardData(root, anchor, id);
    const panel = document.createElement("div"); panel.className = "ns-ozon-analysis ns-ozon-card"; panel.dataset.productId = id;
    const header = document.createElement("header");
    const name = document.createElement("b"); name.textContent = "N · OZON DATA";
    const link = document.createElement("a"); link.href = anchor.href; link.textContent = "详情数据 →";
    header.append(name, link); panel.append(header);
    const body = document.createElement("section");
    addRow(body, "SKU", id, "blue"); addRow(body, "售价", formatRub(data.price), "blue");
    addRow(body, "折扣", percentage(data.price, data.original), data.original ? "red" : "");
    addRow(body, "评分 / 评论", [data.rating, data.reviews].filter(Boolean).join(" / ") || "未公开");
    addRow(body, "页面可见销量", data.sold || "未公开");
    panel.append(body); root.append(panel);
    sellerMetrics(id).then(response => {
      if (!panel.isConnected || !response?.ok) return;
      const metrics = response.data || {};
      addRow(body, "类目 / 品牌", [metric(metrics, "category3", "category_name", "category"), metric(metrics, "brand", "brand_name")].filter(Boolean).join(" / ") || "未公开");
      addRow(body, "月销量 / 销售额", `${formatNumber(metric(metrics, "soldCount", "sold_count"))} / ${formatNumber(metric(metrics, "soldSum", "gmvSum", "revenue"), " ₽")}`, "blue");
      const monthly = metric(metrics, "soldCount", "sold_count");
      addRow(body, "日销量 / 月动态", `${monthly != null ? formatNumber(Number(monthly) / 30) : "未公开"} / ${formatNumber(metric(metrics, "salesDynamics", "sales_dynamics"), "%")}`);
      addRow(body, "广告占比 / 转化率", `${formatNumber(metric(metrics, "drr"), "%")} / ${formatNumber(metric(metrics, "convViewToOrder"), "%")}`);
      addRow(body, "商品卡 / 搜索浏览", `${formatNumber(metric(metrics, "qtyViewPdp", "qty_view_pdp"))} / ${formatNumber(metric(metrics, "sessionCountSearch", "session_count_search"))}`);
      addRow(body, "发货 / 退货率", `${metric(metrics, "salesSchema", "sales_schema") || "未公开"} / ${formatNumber(metric(metrics, "nullableRedemptionRate", "redemption_rate"), "%")}`);
      addRow(body, "佣金 / 上架时间", `${formatNumber(metric(metrics, "commission_rate", "commissionRate"), "%")} / ${metric(metrics, "nullableCreateDate", "created_at", "visible_since") || "未公开"}`);
    });
  }

  function render() {
    const detailId = productId(location.href);
    if (detailId) {
      if (currentDetailId !== detailId || !document.querySelector(`.ns-ozon-detail[data-product-id="${CSS.escape(detailId)}"]`)) { currentDetailId = detailId; detailPanel(detailId); }
      return;
    }
    currentDetailId = ""; document.querySelectorAll(".ns-ozon-detail").forEach(node => node.remove());
    const usedRoots = new Set();
    for (const anchor of document.querySelectorAll('a[href*="/product/"]')) {
      const id = productId(anchor.href); if (!id) continue;
      const root = cardRoot(anchor); if (!root || usedRoots.has(root)) continue;
      usedRoots.add(root); addCardPanel(root, anchor, id);
    }
  }

  function schedule() { clearTimeout(scheduled); scheduled = setTimeout(render, 450); }
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("popstate", schedule); schedule();
})();
