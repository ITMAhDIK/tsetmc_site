/*
 * Public runtime configuration for the GitHub Pages client.
 *
 * Set apiBase to the HTTPS origin (or same-origin proxy base path) only after
 * the Analytics API, same-origin session cookie and privacy notice are live.
 * The SDK appends /api/v1/analytics itself. Never put credentials or secrets here.
 *
 * Empty by default: analytics stays off and no activity is transmitted.
 */
window.TLOOMBERG_ANALYTICS_CONFIG = Object.freeze({
  apiBase: "",
  flushIntervalMs: 10000,
  activeTickMs: 15000,
  maxQueueSize: 200
});
