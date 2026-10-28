(() => {
  if (window.__northstar1688ProbeInstalled) return;
  window.__northstar1688ProbeInstalled = true;

  const capturedResponses = [];
  const interestingKey = /(?:offer|product|detail|sku|spec|price|freight|logistics|shipping|delivery|image|attribute)/i;

  function parseJsonish(text) {
    try { return JSON.parse(text); } catch (_) {}
    const start = String(text || "").indexOf("{");
    const end = String(text || "").lastIndexOf("}");
    if (start >= 0 && end > start) {
      try { return JSON.parse(String(text).slice(start, end + 1)); } catch (_) {}
    }
    return null;
  }

  function remember(value, url = "") {
    if (!value || typeof value !== "object") return;
    capturedResponses.push({ url: String(url || "").slice(0, 1000), value });
    if (capturedResponses.length > 80) capturedResponses.splice(0, capturedResponses.length - 80);
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = async function (...args) {
      const response = await nativeFetch.apply(this, args);
      try {
        const url = response.url || String(args[0] || "");
        if (interestingKey.test(url)) response.clone().text().then(text => remember(parseJsonish(text), url)).catch(() => {});
      } catch (_) {}
      return response;
    };
  }

  const nativeOpen = XMLHttpRequest.prototype.open;
  const nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__northstarUrl = String(url || "");
    return nativeOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        if (!interestingKey.test(this.__northstarUrl || "")) return;
        const value = this.responseType === "json" ? this.response : parseJsonish(this.responseText || "");
        remember(value, this.__northstarUrl);
      } catch (_) {}
    }, { once: true });
    return nativeSend.apply(this, args);
  };

  function cloneCandidate(value, state, depth = 0) {
    if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
      return typeof value === "string" ? value.slice(0, 5000) : value;
    }
    if (!value || typeof value !== "object" || depth > 8 || state.nodes > 2500) return null;
    if (state.seen.has(value)) return null;
    state.seen.add(value);
    state.nodes += 1;
    if (Array.isArray(value)) return value.slice(0, 600).map(item => cloneCandidate(item, state, depth + 1));
    const output = {};
    for (const key of Object.keys(value).slice(0, 500)) {
      if (key === "__proto__" || key === "prototype" || key === "constructor") continue;
      let item;
      try { item = value[key]; } catch (_) { continue; }
      if (typeof item === "function" || typeof item === "symbol") continue;
      output[key] = cloneCandidate(item, state, depth + 1);
    }
    return output;
  }

  function relevantObjects(roots) {
    const result = [];
    const stack = roots.filter(Boolean);
    const seen = new Set();
    let visited = 0;
    while (stack.length && visited < 120000 && result.length < 260) {
      const value = stack.pop();
      if (!value || typeof value !== "object" || seen.has(value)) continue;
      seen.add(value);
      visited += 1;
      const keys = Array.isArray(value) ? [] : Object.keys(value);
      if (keys.some(key => interestingKey.test(key))) {
        const cloned = cloneCandidate(value, { seen: new Set(), nodes: 0 });
        if (cloned) result.push(cloned);
      }
      let children = [];
      try { children = Array.isArray(value) ? value : Object.values(value); } catch (_) { children = []; }
      for (const child of children) if (child && typeof child === "object") stack.push(child);
    }
    return result;
  }

  function componentRoots() {
    const roots = [];
    const selectors = ["#app", "#root", "[class*='sku']", "[class*='spec']", "[class*='price']", "[class*='freight']"];
    for (const element of document.querySelectorAll(selectors.join(","))) {
      for (const key of Object.getOwnPropertyNames(element)) {
        if (key.startsWith("__reactProps$") || key.startsWith("__reactFiber$") || key === "__vueParentComponent") {
          let current = element[key];
          for (let level = 0; current && level < 10; level += 1) {
            roots.push(current.memoizedProps, current.pendingProps, current.memoizedState, current.props, current.ctx);
            current = current.return || current.parent;
          }
        }
      }
      if (roots.length > 300) break;
    }
    return roots.filter(Boolean);
  }

  window.addEventListener("message", event => {
    if (event.source !== window || event.data?.source !== "northstar-1688-collector" || event.data?.type !== "SCAN_MAIN") return;
    const knownNames = [
      "__INIT_DATA__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "__GLOBAL_DATA__",
      "__NEXT_DATA__", "__NUXT__", "__ICE_APP_DATA__", "__APOLLO_STATE__", "__STORE__"
    ];
    const known = knownNames.map(name => {
      try { return window[name]; } catch (_) { return null; }
    }).filter(Boolean);
    const roots = [...capturedResponses.map(item => item.value), ...known, ...componentRoots()];
    window.postMessage({
      source: "northstar-1688-collector",
      type: "SCAN_MAIN_RESULT",
      nonce: event.data.nonce,
      candidates: relevantObjects(roots),
      response_urls: capturedResponses.map(item => item.url).filter(Boolean).slice(-30),
    }, "*");
  });
})();
