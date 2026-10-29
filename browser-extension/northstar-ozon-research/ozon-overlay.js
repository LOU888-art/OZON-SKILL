(() => {
  if (window.__northstarOzonOverlayInstalled) return;
  window.__northstarOzonOverlayInstalled = true;

  const PRODUCT_PATH = /\/product\/(?:[^/?#]+-)?(\d{5,})(?:\/|[?#]|$)/i;
  let scheduled = null;
  let currentDetailId = "";

  function clean(value) {
    return String(value ?? "").replace(/[\u00a0\u2009\u202f]/g, " ").replace(/\s+/g, " ").trim();
  }

  function productId(value) {
    try { return new URL(value, location.origin).pathname.match(PRODUCT_PATH)?.[1] || ""; }
    catch (_) { return String(value || "").match(PRODUCT_PATH)?.[1] || ""; }
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

  function percentage(current, original) {
    if (!current || !original || original <= current) return "—";
    return `-${Math.round((1 - current / original) * 100)}%`;
  }

  function firstMatch(text, patterns) {
    for (const pattern of patterns) {
      const match = text.match(pattern);
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

  function breadcrumbCategory() {
    const values = [...document.querySelectorAll('nav a[href], [data-widget*="bread" i] a[href], a[href*="/category/"]')]
      .map(node => clean(node.textContent))
      .filter(value => value && value.length < 80 && !/главная|ozon/i.test(value));
    return values.at(-1) || "未公开";
  }

  function visibleProductText() {
    const main = document.querySelector("main") || document.body;
    return clean(main?.innerText || "").slice(0, 180000);
  }

  function detailData(id) {
    const schema = schemaProduct() || {};
    const text = visibleProductText();
    const offer = Array.isArray(schema.offers) ? schema.offers[0] : (schema.offers || {});
    const rating = schema.aggregateRating || {};
    const pagePrices = [...text.matchAll(/([\d\s\u00a0\u2009\u202f]+)\s*₽/g)]
      .map(match => Number(match[1].replace(/\s/g, "")))
      .filter(value => Number.isFinite(value) && value > 0);
    const price = Number(offer.price) || pagePrices[0] || null;
    const original = pagePrices.find(value => price && value > price) || null;
    const brandValue = typeof schema.brand === "object" ? schema.brand?.name : schema.brand;
    const brand = clean(brandValue) || firstMatch(text, [/(?:Бренд|品牌)\s*[:：]?\s*([^\n]{1,80})/i]);
    const dimensions = firstMatch(text, [
      /(?:Габариты|Размеры|Длина\s*[×xх]\s*Ширина\s*[×xх]\s*Высота)\s*[:：]?\s*(\d+(?:[.,]\d+)?\s*[×xх]\s*\d+(?:[.,]\d+)?\s*[×xх]\s*\d+(?:[.,]\d+)?\s*(?:мм|см|mм|mm)?)/i,
      /(\d+(?:[.,]\d+)?\s*[×xх]\s*\d+(?:[.,]\d+)?\s*[×xх]\s*\d+(?:[.,]\d+)?\s*(?:мм|см|mm))/i,
    ]);
    const weight = firstMatch(text, [/(?:Вес товара|Вес|重量)\s*[:：]?\s*(\d+(?:[.,]\d+)?\s*(?:г|кг|g|kg))/i]);
    const seller = firstMatch(text, [/(?:Продавец|卖家)\s*[:：]?\s*([^\n]{2,100})/i]);
    const sold = firstMatch(text, [/(\d[\d\s+.,]*(?:тыс\.?)?)\s*(?:купили|продано|заказали)/i]);
    const delivery = firstMatch(text, [/(?:Доставка|Получение)\s*[:：]?\s*([^\n]{2,120})/i]);
    const title = clean(schema.name) || clean(document.querySelector("h1")?.textContent) || clean(document.title);
    return {
      id,
      title,
      category: breadcrumbCategory(),
      brand: brand || "未公开",
      price,
      original,
      discount: percentage(price, original),
      rating: clean(rating.ratingValue) || firstMatch(text, [/(?:Рейтинг|评分)\s*[:：]?\s*(\d(?:[.,]\d)?)/i]),
      reviews: clean(rating.reviewCount || rating.ratingCount) || firstMatch(text, [/(\d[\d\s]*)\s*(?:отзыв|оценк)/i]),
      sold: sold || "未公开",
      seller: seller || "未公开",
      dimensions: dimensions || "未公开",
      weight: weight || "未公开",
      delivery: delivery || "未公开",
    };
  }

  function detailPanel(id) {
    document.querySelectorAll(".ns-ozon-detail").forEach(node => node.remove());
    const data = detailData(id);
    const panel = document.createElement("aside");
    panel.className = "ns-ozon-analysis ns-ozon-detail";
    panel.dataset.productId = id;
    const header = document.createElement("header");
    const title = document.createElement("b");
    title.textContent = "NORTHSTAR · OZON DATA";
    const controls = document.createElement("div");
    const refresh = document.createElement("button");
    refresh.type = "button"; refresh.title = "刷新"; refresh.textContent = "↻";
    refresh.onclick = () => detailPanel(id);
    const toggle = document.createElement("button");
    toggle.type = "button"; toggle.title = "收起/展开"; toggle.textContent = "−";
    controls.append(refresh, toggle); header.append(title, controls); panel.append(header);
    const heading = document.createElement("div");
    heading.className = "ns-ozon-title"; heading.textContent = data.title;
    const body = document.createElement("section");
    addRow(body, "类目", data.category);
    addRow(body, "SKU", data.id, "blue");
    addRow(body, "品牌", data.brand, "blue");
    addRow(body, "当前售价", formatRub(data.price), "blue");
    addRow(body, "原价", formatRub(data.original));
    addRow(body, "折扣", data.discount, data.discount !== "—" ? "red" : "");
    addRow(body, "评分", data.rating || "未公开");
    addRow(body, "评论数", data.reviews || "未公开");
    addRow(body, "页面可见销量", data.sold);
    addRow(body, "卖家", data.seller);
    addRow(body, "配送", data.delivery);
    addRow(body, "长×宽×高", data.dimensions);
    addRow(body, "重量", data.weight);
    addRow(body, "类目佣金", "需要 Ozon Seller 授权", "muted");
    addRow(body, "月销量 / 月销售额", "公开页未提供", "muted");
    addRow(body, "广告占比 / 加购率", "公开页未提供", "muted");
    addRow(body, "转化率 / 退货率", "公开页未提供", "muted");
    const note = document.createElement("footer");
    note.textContent = "数据来自当前 Ozon 页面与结构化商品标记，不使用虚构估算值。";
    panel.append(heading, body, note);
    toggle.onclick = () => {
      const collapsed = panel.classList.toggle("collapsed");
      toggle.textContent = collapsed ? "+" : "−";
    };
    document.body.append(panel);
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
    const prices = [...text.matchAll(/([\d\s\u00a0\u2009\u202f]+)\s*₽/g)]
      .map(match => Number(match[1].replace(/\s/g, "")))
      .filter(value => Number.isFinite(value) && value > 0);
    const price = prices[0] || null;
    const original = prices.find(value => price && value > price) || null;
    const title = clean(anchor.textContent) || clean(root.querySelector("img")?.alt);
    const rating = firstMatch(text, [/(\d(?:[.,]\d))\s*(?:★|⭐|·|\s)/]);
    const reviews = firstMatch(text, [/(\d[\d\s]*)\s*(?:отзыв|оценк)/i]);
    const sold = firstMatch(text, [/(\d[\d\s+.,]*(?:тыс\.?)?)\s*(?:купили|продано|заказали)/i]);
    return { id, title, price, original, rating, reviews, sold };
  }

  function addCardPanel(root, anchor, id) {
    if (root.querySelector(`.ns-ozon-card[data-product-id="${CSS.escape(id)}"]`)) return;
    const data = cardData(root, anchor, id);
    const panel = document.createElement("div");
    panel.className = "ns-ozon-analysis ns-ozon-card";
    panel.dataset.productId = id;
    const header = document.createElement("header");
    const name = document.createElement("b"); name.textContent = "N · OZON DATA";
    const link = document.createElement("a"); link.href = anchor.href; link.textContent = "详情数据 →";
    header.append(name, link); panel.append(header);
    const body = document.createElement("section");
    addRow(body, "SKU", id, "blue");
    addRow(body, "售价", formatRub(data.price), "blue");
    addRow(body, "折扣", percentage(data.price, data.original), data.original ? "red" : "");
    addRow(body, "评分 / 评论", [data.rating, data.reviews].filter(Boolean).join(" / ") || "未公开");
    addRow(body, "页面可见销量", data.sold || "未公开");
    panel.append(body);
    root.append(panel);
  }

  function render() {
    const detailId = productId(location.href);
    if (detailId) {
      if (currentDetailId !== detailId || !document.querySelector(`.ns-ozon-detail[data-product-id="${CSS.escape(detailId)}"]`)) {
        currentDetailId = detailId;
        detailPanel(detailId);
      }
      return;
    }
    currentDetailId = "";
    document.querySelectorAll('.ns-ozon-detail').forEach(node => node.remove());
    const usedRoots = new Set();
    for (const anchor of document.querySelectorAll('a[href*="/product/"]')) {
      const id = productId(anchor.href);
      if (!id) continue;
      const root = cardRoot(anchor);
      if (!root || usedRoots.has(root)) continue;
      usedRoots.add(root);
      addCardPanel(root, anchor, id);
    }
  }

  function schedule() {
    clearTimeout(scheduled);
    scheduled = setTimeout(render, 450);
  }

  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("popstate", schedule);
  schedule();
})();
