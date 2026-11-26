/* Consent-aware Tloomberg product analytics. No third-party tracker is used. */
(() => {
  "use strict";

  const API_BASE = String(window.SITE_API_BASE_URL || "").replace(/\/$/, "");
  const MAX_BATCH = 50;
  const FLUSH_INTERVAL_MS = 12000;
  const ACTIVE_INTERVAL_MS = 15000;
  const ALLOWED_PAGE = /^\/[\p{L}\p{N}_./:-]{1,300}$/u;
  const state = {
    enabled: false,
    pagePath: null,
    queue: [],
    activeSince: null,
    scrollDepth: 0,
    flushTimer: null,
    activeTimer: null,
    listenersAttached: false,
    flushing: false,
    transportUnavailable: false,
  };

  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = Math.random() * 16 | 0;
      return (c === "x" ? r : (r & 3 | 8)).toString(16);
    });
  }

  function isActive() {
    return state.enabled &&
      document.visibilityState === "visible" &&
      (!document.hasFocus || document.hasFocus());
  }

  function enqueue(eventName, fields) {
    if (!state.enabled || !state.pagePath || state.transportUnavailable) return;
    state.queue.push({
      client_event_id: uuid(),
      event_name: eventName,
      page_path: state.pagePath,
      ...fields,
    });
    if (state.queue.length >= 10) void flush();
  }

  async function flush() {
    if (
      state.flushing || !state.enabled || state.transportUnavailable ||
      state.queue.length === 0
    ) return;
    state.flushing = true;
    try {
      while (state.queue.length && state.enabled) {
        const batch = state.queue.slice(0, MAX_BATCH);
        const response = await fetch(API_BASE + "/api/v1/analytics/events", {
          method: "POST",
          credentials: "include",
          keepalive: true,
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": String(window.__siteCsrf || ""),
          },
          body: JSON.stringify({ events: batch }),
        });
        if (!response.ok) {
          // A missing backend or revoked consent must not create a retry storm.
          state.queue.length = 0;
          if (response.status === 401 || response.status === 403 || response.status === 404) {
            state.transportUnavailable = true;
          }
          break;
        }
        state.queue.splice(0, batch.length);
      }
    } catch (_) {
      // Keep the small in-memory batch for the next interval; never persist it
      // Keep the in-memory batch only; never persist behavior in browser storage.
    } finally {
      state.flushing = false;
    }
  }

  function flushActiveTime() {
    if (!state.activeSince) return;
    const duration = Math.max(0, Math.min(180000, Date.now() - state.activeSince));
    state.activeSince = isActive() ? Date.now() : null;
    if (duration >= 1000) {
      enqueue("active_time", { active_duration_ms: duration });
    }
  }

  function pageChanged(path) {
    if (!state.enabled) return;
    flushActiveTime();
    const clean = String(path || "").split(/[?#]/, 1)[0];
    if (!ALLOWED_PAGE.test(clean) || clean.includes("..")) return;
    if (clean === state.pagePath) return;
    state.pagePath = clean;
    state.scrollDepth = 0;
    enqueue("page_view", {});
    if (isActive()) state.activeSince = Date.now();
    void flush();
  }

  function onClick(event) {
    if (!state.enabled || !state.pagePath) return;
    const target = event.target && event.target.closest
      ? event.target.closest("[data-analytics-id],button,a,[role='button'],[role='tab'],[role='menuitem']")
      : null;
    if (!target || target.closest("[data-analytics-exclude]")) return;

    const declaredId = String(target.getAttribute("data-analytics-id") || "");
    const rawRole = String(
      target.getAttribute("data-analytics-role") ||
      target.getAttribute("role") ||
      (target.tagName === "A" ? "link" : "button")
    ).toLowerCase();
    const role = ["button", "link", "tab", "navigation", "menuitem"].includes(rawRole)
      ? rawRole
      : "other";

    // Only use a developer-declared component ID. For unlabelled controls,
    // retain a generic role bucket; never inspect text, titles, values or labels.
    const componentId = /^[A-Za-z0-9_-]{1,80}$/.test(declaredId)
      ? declaredId
      : "interactive-" + role;
    enqueue("click", { component_id: componentId, element_role: role });
  }

  function onScroll() {
    if (!state.enabled || !state.pagePath || !isActive()) return;
    const doc = document.documentElement;
    const maxScroll = Math.max(1, doc.scrollHeight - window.innerHeight);
    const percent = Math.max(0, Math.min(100, Math.round(window.scrollY / maxScroll * 100)));
    const threshold = Math.floor(percent / 25) * 25;
    if (threshold >= 25 && threshold > state.scrollDepth) {
      state.scrollDepth = threshold;
      enqueue("scroll", { scroll_depth_pct: threshold });
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState === "hidden") {
      flushActiveTime();
      void flush();
      state.activeSince = null;
    } else if (isActive() && state.enabled) {
      state.activeSince = Date.now();
    }
  }

  function attachListeners() {
    if (!state.listenersAttached) {
      state.listenersAttached = true;
      document.addEventListener("click", onClick, true);
      window.addEventListener("scroll", onScroll, { passive: true });
      document.addEventListener("visibilitychange", onVisibilityChange);
      window.addEventListener("blur", () => {
        flushActiveTime();
        state.activeSince = null;
      });
      window.addEventListener("focus", () => {
        if (state.enabled && isActive() && !state.activeSince) {
          state.activeSince = Date.now();
        }
      });
      window.addEventListener("pagehide", () => {
        flushActiveTime();
        void flush();
      });
    }
    if (!state.flushTimer) {
      state.flushTimer = window.setInterval(() => void flush(), FLUSH_INTERVAL_MS);
    }
    if (!state.activeTimer) {
      state.activeTimer = window.setInterval(flushActiveTime, ACTIVE_INTERVAL_MS);
    }
  }

  function setEnabled(enabled) {
    state.enabled = enabled === true;
    state.transportUnavailable = false;
    if (!state.enabled) {
      state.queue.length = 0;
      state.pagePath = null;
      state.activeSince = null;
      state.scrollDepth = 0;
      if (state.flushTimer) window.clearInterval(state.flushTimer);
      if (state.activeTimer) window.clearInterval(state.activeTimer);
      state.flushTimer = null;
      state.activeTimer = null;
      return;
    }
    attachListeners();
    if (isActive()) state.activeSince = Date.now();
  }

  window.TloombergAnalytics = Object.freeze({
    setEnabled,
    setPage: pageChanged,
    flush,
  });
})();
