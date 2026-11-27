(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) {
    const client = api.createAnalyticsSdk({
      windowRef: root,
      documentRef: root.document,
      config: root.TLOOMBERG_ANALYTICS_CONFIG || {}
    });
    root.TloombergAnalytics = client;
    client.init();
  }
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const EVENT_BATCH_SIZE = 50;
  const VALID_PAGES = new Set([
    "market", "stock", "shareholders", "portfolio", "investor-history",
    "screener", "watchlist", "account", "admin"
  ]);
  const SCROLL_STEPS = [25, 50, 75, 100];
  const SAFE_IDENTIFIER = /^[A-Za-z][A-Za-z0-9_-]{0,79}$/;
  const TICKER_SLUG = /^[\p{L}\p{N}_-]{1,40}$/u;

  function safeIdentifier(value, fallback) {
    return typeof value === "string" && SAFE_IDENTIFIER.test(value)
      ? value
      : fallback;
  }

  function routePath(route) {
    const source = route && typeof route === "object" ? route : {};
    const page = VALID_PAGES.has(source.page) ? source.page : "market";
    if (page === "stock") {
      const symbol = typeof source.activeSymbol === "string"
        ? source.activeSymbol.trim()
        : "";
      return symbol && TICKER_SLUG.test(symbol)
        ? "/stock/" + symbol
        : "/stock";
    }
    // Deliberately omit portfolioInvestor and every user-supplied identifier.
    return "/" + page;
  }

  function makeUuid(win) {
    if (win && win.crypto && typeof win.crypto.randomUUID === "function") {
      return win.crypto.randomUUID();
    }
    const crypto = win && win.crypto;
    const bytes = new Uint8Array(16);
    if (crypto && typeof crypto.getRandomValues === "function") {
      crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i += 1) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" +
      hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }

  function resolveApiBase(raw, origin) {
    if (typeof raw !== "string" || !raw.trim()) return "";
    const value = raw.trim().replace(/\/+$/, "");
    try {
      const parsed = new URL(value, origin || "https://pages.invalid");
      if (parsed.protocol !== "https:" && parsed.origin !== origin) return "";
      if (parsed.username || parsed.password || parsed.search || parsed.hash) return "";
      if (value.startsWith("//")) return "";
      return value;
    } catch (_) {
      return "";
    }
  }

  function createAnalyticsSdk(options) {
    const opts = options || {};
    const win = opts.windowRef || (typeof window !== "undefined" ? window : {});
    const doc = opts.documentRef || win.document || null;
    const config = opts.config || win.TLOOMBERG_ANALYTICS_CONFIG || {};
    const fetchImpl = opts.fetchImpl || (typeof win.fetch === "function" ? win.fetch.bind(win) : null);
    const origin = win.location && win.location.origin ? win.location.origin : "";
    const apiBase = resolveApiBase(config.apiBase, origin);
    const flushIntervalMs = Number.isInteger(config.flushIntervalMs)
      ? Math.max(5000, Math.min(config.flushIntervalMs, 60000))
      : 10000;
    const activeTickMs = Number.isInteger(config.activeTickMs)
      ? Math.max(5000, Math.min(config.activeTickMs, 60000))
      : 15000;
    const maxQueueSize = Number.isInteger(config.maxQueueSize)
      ? Math.max(EVENT_BATCH_SIZE, Math.min(config.maxQueueSize, 1000))
      : 200;

    let enabled = false;
    let initialized = false;
    let inFlight = false;
    let consentState = "unknown";
    let initialPageTracked = false;
    let currentRoute = win.TLOOMBERG_ANALYTICS_INITIAL_ROUTE || { page: "market" };
    let currentPath = routePath(currentRoute);
    let queue = [];
    let timer = null;
    let scrollHandler = null;
    let clickHandler = null;
    let visibilityHandler = null;
    let pageHideHandler = null;
    let banner = null;
    let settingsButton = null;
    const scrollReported = new Set();
    const dismissedKey = "tloomberg-analytics-dismissed-v1";

    function isDismissed() {
      try {
        return Boolean(win.sessionStorage && win.sessionStorage.getItem(dismissedKey) === "1");
      } catch (_) {
        return false;
      }
    }

    function setDismissed(value) {
      try {
        if (!win.sessionStorage) return;
        if (value) win.sessionStorage.setItem(dismissedKey, "1");
        else win.sessionStorage.removeItem(dismissedKey);
      } catch (_) {}
    }

    async function request(path, method, body, keepalive) {
      if (!apiBase || !fetchImpl) throw new Error("analytics_api_not_configured");
      const response = await fetchImpl(apiBase + path, {
        method: method || "GET",
        credentials: "include",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        keepalive: Boolean(keepalive)
      });
      if (!response.ok) {
        const error = new Error("analytics_http_" + response.status);
        error.status = response.status;
        throw error;
      }
      return response.status === 204 ? {} : response.json();
    }

    function enqueue(eventName, fields) {
      if (!enabled) return false;
      const allowed = {
        client_event_id: makeUuid(win),
        event_name: eventName,
        page_path: currentPath
      };
      const input = fields || {};
      if (input.component_id) {
        allowed.component_id = safeIdentifier(input.component_id, "other");
      }
      if (input.element_role) {
        allowed.element_role = safeIdentifier(input.element_role, "other");
      }
      if (eventName === "scroll" && Number.isFinite(input.scroll_depth_pct)) {
        allowed.scroll_depth_pct = Math.max(0, Math.min(100, input.scroll_depth_pct));
      }
      if (eventName === "active_time" && Number.isInteger(input.active_duration_ms)) {
        allowed.active_duration_ms = Math.max(0, Math.min(180000, input.active_duration_ms));
      }
      if (eventName === "click" &&
          Number.isFinite(input.x_pct) && Number.isFinite(input.y_pct)) {
        allowed.x_pct = Math.max(0, Math.min(100, input.x_pct));
        allowed.y_pct = Math.max(0, Math.min(100, input.y_pct));
      }
      queue.push(allowed);
      if (queue.length > maxQueueSize) queue = queue.slice(-maxQueueSize);
      if (queue.length >= EVENT_BATCH_SIZE) void flush();
      return true;
    }

    async function flush() {
      if (!enabled || !apiBase || !queue.length || inFlight) return false;
      inFlight = true;
      const batch = queue.slice(0, EVENT_BATCH_SIZE);
      try {
        await request("/api/v1/analytics/events", "POST", { events: batch }, true);
        queue.splice(0, batch.length);
        return true;
      } catch (error) {
        if (error && (error.status === 401 || error.status === 403)) {
          enabled = false;
          consentState = "login-required";
          queue = [];
          stopCollectors();
          showConsentBanner("login-required");
        }
        return false;
      } finally {
        inFlight = false;
      }
    }

    function trackPage(route, options) {
      const nextRoute = route && typeof route === "object" ? { ...route } : { page: "market" };
      const nextPath = routePath(nextRoute);
      const changed = nextPath !== currentPath;
      currentRoute = nextRoute;
      currentPath = nextPath;
      scrollReported.clear();
      if (!enabled) return false;
      const isInitial = Boolean(options && options.initial);
      if (isInitial && initialPageTracked) return false;
      if (!isInitial && !changed) return false;
      if (isInitial) initialPageTracked = true;
      enqueue("navigation", { component_id: "route-change", element_role: "navigation" });
      enqueue("page_view");
      const pageName = VALID_PAGES.has(currentRoute.page) ? currentRoute.page : "market";
      enqueue("feature_view", {
        component_id: "feature-" + pageName,
        element_role: "feature"
      });
      return true;
    }

    function trackClick(componentId, elementRole, xPercent, yPercent) {
      const component = safeIdentifier(componentId, "other");
      const role = safeIdentifier(elementRole, "other");
      const fields = { component_id: component, element_role: role };
      if (Number.isFinite(xPercent) && Number.isFinite(yPercent)) {
        fields.x_pct = Math.max(0, Math.min(100, xPercent));
        fields.y_pct = Math.max(0, Math.min(100, yPercent));
      }
      return enqueue("click", fields);
    }

    function trackFeature(featureId) {
      const feature = safeIdentifier(featureId, "");
      if (!feature) return false;
      return enqueue("feature_view", {
        component_id: feature,
        element_role: "feature"
      });
    }

    function trackScroll(depthPercent) {
      if (!Number.isFinite(depthPercent)) return false;
      const rounded = SCROLL_STEPS.find(step => depthPercent >= step && !scrollReported.has(step));
      if (!rounded) return false;
      scrollReported.add(rounded);
      return enqueue("scroll", { scroll_depth_pct: rounded });
    }

    function trackActiveTime(durationMs) {
      if (!Number.isInteger(durationMs) || durationMs < 0 || durationMs > 180000) return false;
      return enqueue("active_time", { active_duration_ms: durationMs });
    }

    function onClick(event) {
      const target = event && event.target;
      if (!target || typeof target.closest !== "function") return;
      if (target.closest("#tloomberg-analytics-consent, #tloomberg-analytics-settings")) return;
      if (target.closest("input, textarea, select, option, [contenteditable='true']")) return;
      const element = target.closest("[data-analytics-id], button, a, [role='button'], [role='tab']");
      if (!element) return;
      const tag = (element.tagName || "").toLowerCase();
      const roleValue = element.getAttribute && element.getAttribute("role");
      const role = safeIdentifier(roleValue, tag === "a" ? "link" : tag === "button" ? "button" : "other");
      const component = element.getAttribute
        ? safeIdentifier(element.getAttribute("data-analytics-id"), "")
        : "";
      // No textContent, href, form value, URL query or user-entered text is read.
      // Coordinates are viewport-relative percentages, never DOM positions or content.
      const width = Number(win.innerWidth) || 0;
      const height = Number(win.innerHeight) || 0;
      const xPct = width > 0 && Number.isFinite(event.clientX)
        ? (event.clientX / width) * 100 : undefined;
      const yPct = height > 0 && Number.isFinite(event.clientY)
        ? (event.clientY / height) * 100 : undefined;
      trackClick(
        component || (tag === "a" ? "link" : tag === "button" ? "button" : "control"),
        role, xPct, yPct
      );
    }

    function onScroll() {
      if (!doc || !doc.documentElement) return;
      const rootHeight = Math.max(
        doc.documentElement.scrollHeight || 0,
        doc.body ? doc.body.scrollHeight || 0 : 0
      );
      const scrollable = rootHeight - (win.innerHeight || 0);
      const depth = scrollable <= 0 ? 100 : ((win.scrollY || 0) / scrollable) * 100;
      for (const step of SCROLL_STEPS) {
        if (depth >= step && !scrollReported.has(step)) trackScroll(step);
      }
    }

    function isPageActive() {
      if (!doc || doc.visibilityState === "hidden") return false;
      return typeof doc.hasFocus !== "function" || doc.hasFocus();
    }

    function startCollectors() {
      if (timer !== null) return;
      if (doc && typeof doc.addEventListener === "function") {
        clickHandler = onClick;
        scrollHandler = onScroll;
        visibilityHandler = () => { if (doc.visibilityState === "hidden") void flush(); };
        doc.addEventListener("click", clickHandler, true);
        doc.addEventListener("scroll", scrollHandler, { passive: true });
        doc.addEventListener("visibilitychange", visibilityHandler);
      }
      pageHideHandler = () => { void flush(); };
      if (win && typeof win.addEventListener === "function") {
        win.addEventListener("pagehide", pageHideHandler);
      }
      if (win && typeof win.setInterval === "function") {
        timer = win.setInterval(() => {
          if (enabled && isPageActive()) trackActiveTime(activeTickMs);
          if (queue.length) void flush();
        }, activeTickMs);
      }
    }

    function stopCollectors() {
      if (timer !== null && win && typeof win.clearInterval === "function") {
        win.clearInterval(timer);
      }
      timer = null;
      if (doc && typeof doc.removeEventListener === "function") {
        if (clickHandler) doc.removeEventListener("click", clickHandler, true);
        if (scrollHandler) doc.removeEventListener("scroll", scrollHandler);
        if (visibilityHandler) doc.removeEventListener("visibilitychange", visibilityHandler);
      }
      if (win && pageHideHandler && typeof win.removeEventListener === "function") {
        win.removeEventListener("pagehide", pageHideHandler);
      }
      clickHandler = scrollHandler = visibilityHandler = pageHideHandler = null;
    }

    function removeBanner() {
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
      banner = null;
    }

    function renderSettingsButton() {
      if (!doc || !doc.body || settingsButton) return;
      settingsButton = doc.createElement("button");
      settingsButton.id = "tloomberg-analytics-settings";
      settingsButton.type = "button";
      settingsButton.className = "tloomberg-analytics-settings";
      settingsButton.textContent = "حریم خصوصی";
      settingsButton.setAttribute("aria-label", "تنظیمات حریم خصوصی و تحلیل رفتار");
      settingsButton.addEventListener("click", () => showConsentBanner(consentState));
      doc.body.appendChild(settingsButton);
    }

    function showConsentBanner(state) {
      if (!doc || !doc.body) return;
      removeBanner();
      const panel = doc.createElement("section");
      panel.id = "tloomberg-analytics-consent";
      panel.className = "tloomberg-analytics-consent";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "false");
      panel.setAttribute("aria-labelledby", "tloomberg-analytics-title");
      panel.setAttribute("data-analytics-ignore", "true");

      const title = doc.createElement("h2");
      title.id = "tloomberg-analytics-title";
      title.textContent = "حریم خصوصی و بهبود تجربه تلومبرگ";

      const description = doc.createElement("p");
      description.textContent = "با اجازه شما، صفحات مشاهده‌شده، کلیک روی اجزای مشخص رابط، میزان اسکرول، مسیر پیمایش و مدت فعالیت ثبت می‌شود. متن کلیک‌ها، مقدار فرم‌ها، رمزها، اطلاعات بانکی، آدرس کامل و محتوای خصوصی جمع‌آوری نمی‌شود. رضایت شما در هر زمان قابل لغو است.";

      const status = doc.createElement("p");
      status.className = "tloomberg-analytics-status";
      if (!apiBase) {
        status.textContent = "اتصال سرور تحلیل هنوز پیکربندی نشده است؛ در وضعیت فعلی هیچ فعالیتی به جایی ارسال نمی‌شود.";
      } else if (state === "login-required") {
        status.textContent = "ثبت فعالیت اختصاصی فقط برای حساب واردشده امکان‌پذیر است. تا پیش از ورود و ثبت رضایت، هیچ رویدادی ذخیره نمی‌شود.";
      } else if (state === "error") {
        status.textContent = "ارتباط با سرویس تحلیل برقرار نشد. تا رفع مشکل، ثبت فعالیت غیرفعال است.";
      } else if (state === "accepted") {
        status.textContent = "رضایت تحلیل رفتار شما فعال است؛ برای توقف ثبت، «موافق نیستم» را انتخاب کنید.";
      } else {
        status.textContent = "هیچ فعالیتی پیش از موافقت صریح شما ثبت نمی‌شود.";
      }

      const actions = doc.createElement("div");
      actions.className = "tloomberg-analytics-actions";
      const accept = doc.createElement("button");
      accept.type = "button";
      accept.className = "tloomberg-analytics-accept";
      accept.textContent = state === "accepted" ? "رضایت فعال بماند" : "موافقت می‌کنم";
      accept.disabled = !apiBase;
      accept.addEventListener("click", async () => {
        accept.disabled = true;
        try {
          await setConsent(true);
          setDismissed(false);
          removeBanner();
          renderSettingsButton();
        } catch (error) {
          status.textContent = error && error.status === 401
            ? "برای ثبت رضایت باید ابتدا وارد حساب کاربری شوید. هیچ رویدادی ثبت نشده است."
            : "رضایت ثبت نشد؛ تا اتصال موفق به سرور، هیچ فعالیتی جمع‌آوری نمی‌شود.";
          accept.disabled = !apiBase;
        }
      });

      const reject = doc.createElement("button");
      reject.type = "button";
      reject.className = "tloomberg-analytics-reject";
      reject.textContent = "موافق نیستم";
      reject.addEventListener("click", async () => {
        const result = await setConsent(false);
        if (apiBase && !result.stored) {
          status.textContent = "ثبت لغو رضایت به سرور نرسید. جمع‌آوری در این مرورگر متوقف شد؛ برای حذف داده‌های قبلی، پس از برقراری ارتباط دوباره تلاش کنید.";
          return;
        }
        setDismissed(true);
        removeBanner();
        renderSettingsButton();
      });

      const details = doc.createElement("button");
      details.type = "button";
      details.className = "tloomberg-analytics-details";
      details.textContent = "جزئیات";
      details.addEventListener("click", () => {
        status.textContent = "رویدادها فقط پس از رضایت فعال و نشست احراز هویت‌شده ارسال می‌شوند. داده‌های فردی فقط برای نقش دارای مجوز ویژه قابل مشاهده‌اند و مشاهده آن‌ها ثبت ممیزی می‌شود.";
      });

      actions.appendChild(accept);
      actions.appendChild(reject);
      actions.appendChild(details);
      panel.appendChild(title);
      panel.appendChild(description);
      panel.appendChild(status);
      panel.appendChild(actions);
      doc.body.appendChild(panel);
      banner = panel;
      renderSettingsButton();
    }

    async function setConsent(allow) {
      if (allow !== true && allow !== false) throw new TypeError("consent must be boolean");
      if (!allow) {
        enabled = false;
        consentState = "denied";
        queue = [];
        stopCollectors();
        if (!apiBase) return { stored: false, product_analytics: false };
        try {
          const result = await request("/api/v1/analytics/consent", "PUT", { product_analytics: false });
          return { stored: true, product_analytics: Boolean(result.consent && result.consent.product_analytics) };
        } catch (_) {
          return { stored: false, product_analytics: false };
        }
      }
      if (!apiBase) {
        const error = new Error("analytics_api_not_configured");
        error.code = "analytics_api_not_configured";
        throw error;
      }
      const result = await request("/api/v1/analytics/consent", "PUT", { product_analytics: true });
      if (!result.consent || result.consent.product_analytics !== true) {
        throw new Error("analytics_consent_not_confirmed");
      }
      enabled = true;
      consentState = "accepted";
      startCollectors();
      if (currentRoute) trackPage(currentRoute, { initial: true });
      return result;
    }

    async function init() {
      if (initialized) return getState();
      initialized = true;
      if (!apiBase) {
        consentState = "unconfigured";
        return getState();
      }
      renderSettingsButton();
      if (isDismissed()) {
        consentState = "dismissed";
        return getState();
      }
      try {
        const result = await request("/api/v1/analytics/consent", "GET");
        enabled = Boolean(result.consent && result.consent.product_analytics === true);
        consentState = enabled ? "accepted" : "consent-required";
        if (enabled) {
          startCollectors();
          trackPage(currentRoute, { initial: true });
        } else {
          showConsentBanner(consentState);
        }
      } catch (error) {
        consentState = error && error.status === 401 ? "login-required" : "error";
        showConsentBanner(consentState);
      }
      return getState();
    }

    function getState() {
      return {
        enabled,
        consentState,
        apiConfigured: Boolean(apiBase),
        queuedEvents: queue.length,
        currentPath
      };
    }

    function destroy() {
      stopCollectors();
      queue = [];
      removeBanner();
      if (settingsButton && settingsButton.parentNode) {
        settingsButton.parentNode.removeChild(settingsButton);
      }
      settingsButton = null;
      enabled = false;
    }

    return {
      init,
      setConsent,
      trackPage,
      trackClick,
      trackFeature,
      trackScroll,
      trackActiveTime,
      flush,
      getState,
      destroy
    };
  }

  return { createAnalyticsSdk, routePath, resolveApiBase, safeIdentifier };
});
