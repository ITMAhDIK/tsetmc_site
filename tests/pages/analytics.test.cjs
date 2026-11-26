const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createAnalyticsSdk,
  routePath,
  resolveApiBase,
  safeIdentifier,
} = require("../../docs/analytics.js");

function makeWindow() {
  let timerId = 0;
  const listeners = new Map();
  return {
    location: { origin: "https://pages.example.test" },
    crypto: {
      randomUUID: (() => {
        let n = 0;
        return () => "00000000-0000-4000-8000-" + String(++n).padStart(12, "0");
      })(),
    },
    setInterval: () => ++timerId,
    clearInterval: () => {},
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    _listeners: listeners,
  };
}

function response(status, data) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
  };
}

function makeFetch(initialConsent = false) {
  const calls = [];
  let consent = initialConsent;
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/analytics/consent") && options.method === "GET") {
      return response(200, { consent: { product_analytics: consent } });
    }
    if (url.endsWith("/analytics/consent") && options.method === "PUT") {
      const body = JSON.parse(options.body);
      consent = body.product_analytics;
      return response(200, { consent: { product_analytics: consent } });
    }
    if (url.endsWith("/analytics/events") && options.method === "POST") {
      return response(200, { received: JSON.parse(options.body).events.length });
    }
    return response(404, {});
  };
  return { calls, fetchImpl, getConsent: () => consent };
}

test("route path records page and ticker but never investor identifiers or query strings", () => {
  assert.equal(routePath({ page: "market" }), "/market");
  assert.equal(routePath({ page: "stock", activeSymbol: "فولاد" }), "/stock/فولاد");
  assert.equal(
    routePath({ page: "portfolio", portfolioInvestor: "share_id:123456789" }),
    "/portfolio",
  );
  assert.equal(routePath({ page: "unknown-page" }), "/market");
});

test("API base rejects insecure external origins and accepts HTTPS or same-origin proxy", () => {
  assert.equal(resolveApiBase("http://api.example.test", "https://pages.example.test"), "");
  assert.equal(
    resolveApiBase("https://api.example.test/", "https://pages.example.test"),
    "https://api.example.test",
  );
  assert.equal(
    resolveApiBase("/saas-api", "https://pages.example.test"),
    "/saas-api",
  );
});

test("identifier allow-list never passes raw text or personal content", () => {
  assert.equal(safeIdentifier("nav-market", "other"), "nav-market");
  assert.equal(safeIdentifier("Buy ABC now", "other"), "other");
  assert.equal(safeIdentifier("phone_0912", "other"), "other");
});

test("no API config means no network activity and no event collection", async () => {
  const win = makeWindow();
  const calls = [];
  const sdk = createAnalyticsSdk({
    windowRef: win,
    documentRef: null,
    config: { apiBase: "" },
    fetchImpl: async (...args) => { calls.push(args); return response(200, {}); },
  });

  const state = await sdk.init();
  assert.equal(state.enabled, false);
  assert.equal(state.apiConfigured, false);
  assert.equal(sdk.trackPage({ page: "market" }), false);
  assert.equal(sdk.trackClick("nav-market", "button"), false);
  await assert.rejects(() => sdk.setConsent(true), /analytics_api_not_configured/);
  assert.equal(calls.length, 0);
});

test("events are not sent before server consent and login", async () => {
  const win = makeWindow();
  const { calls, fetchImpl } = makeFetch(false);
  const sdk = createAnalyticsSdk({
    windowRef: win,
    documentRef: null,
    config: { apiBase: "https://api.example.test" },
    fetchImpl,
  });

  const state = await sdk.init();
  assert.equal(state.enabled, false);
  assert.equal(sdk.trackClick("nav-market", "button"), false);
  assert.equal(sdk.getState().queuedEvents, 0);
  assert.equal(calls.some(call => call.url.endsWith("/analytics/events")), false);
});

test("consent enables bounded events and sends only allow-listed fields", async () => {
  const win = makeWindow();
  const { calls, fetchImpl } = makeFetch(false);
  const sdk = createAnalyticsSdk({
    windowRef: win,
    documentRef: null,
    config: { apiBase: "https://api.example.test" },
    fetchImpl,
  });

  await sdk.init();
  await sdk.setConsent(true);
  sdk.trackPage({ page: "stock", activeSymbol: "فولاد" });
  sdk.trackClick("nav-stock", "button");
  sdk.trackScroll(25);
  sdk.trackActiveTime(15000);

  assert.equal(sdk.getState().enabled, true);
  assert.equal(sdk.getState().queuedEvents, 5); // navigation + page_view + 3 interactions
  await sdk.flush();

  const post = calls.find(call => call.url.endsWith("/analytics/events"));
  assert.ok(post);
  assert.equal(post.options.credentials, "include");
  const sent = JSON.parse(post.options.body).events;
  assert.equal(sent.length, 5);
  assert.deepEqual(
    sent.map(event => event.event_name),
    ["navigation", "page_view", "click", "scroll", "active_time"],
  );
  assert.ok(sent.every(event => event.page_path === "/stock/فولاد"));
  assert.ok(sent.every(event => !("metadata" in event)));
  assert.ok(sent.every(event => !("user_id" in event) && !("session_id" in event)));
  assert.equal(sent.find(event => event.event_name === "click").component_id, "nav-stock");
  assert.equal(sent.find(event => event.event_name === "active_time").active_duration_ms, 15000);
  assert.equal(sdk.getState().queuedEvents, 0);
});

test("repeated scroll thresholds are recorded once per page", async () => {
  const win = makeWindow();
  const { fetchImpl } = makeFetch(false);
  const sdk = createAnalyticsSdk({
    windowRef: win,
    documentRef: null,
    config: { apiBase: "https://api.example.test" },
    fetchImpl,
  });

  await sdk.init();
  await sdk.setConsent(true);
  sdk.trackScroll(25);
  sdk.trackScroll(25);
  sdk.trackScroll(60);
  assert.equal(sdk.getState().queuedEvents, 4); // initial route events + 25% + 50%
});

test("revoking consent immediately stops collection and discards queued events", async () => {
  const win = makeWindow();
  const { calls, fetchImpl, getConsent } = makeFetch(false);
  const sdk = createAnalyticsSdk({
    windowRef: win,
    documentRef: null,
    config: { apiBase: "https://api.example.test" },
    fetchImpl,
  });

  await sdk.init();
  await sdk.setConsent(true);
  sdk.trackClick("nav-market", "button");
  await sdk.setConsent(false);

  assert.equal(sdk.getState().enabled, false);
  assert.equal(sdk.getState().queuedEvents, 0);
  assert.equal(getConsent(), false);
  assert.equal(calls.filter(call => call.url.endsWith("/analytics/consent") && call.options.method === "PUT").length, 2);
});

test("401 from consent endpoint leaves tracking disabled", async () => {
  const win = makeWindow();
  const sdk = createAnalyticsSdk({
    windowRef: win,
    documentRef: null,
    config: { apiBase: "https://api.example.test" },
    fetchImpl: async () => response(401, {}),
  });

  const state = await sdk.init();
  assert.equal(state.enabled, false);
  assert.equal(state.consentState, "login-required");
  assert.equal(sdk.trackPage({ page: "market" }), false);
  assert.equal(sdk.getState().queuedEvents, 0);
});
