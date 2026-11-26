/*
 * Public runtime configuration for the GitHub Pages client.
 *
 * Set apiBase to the HTTPS origin (or same-origin reverse-proxy path) of the
 * separately hosted SaaS API only after that host, CORS and session cookies
 * have been configured. Never place credentials or secrets in this file.
 *
 * Empty by default: analytics stays off and no activity is transmitted.
 */
window.TLOOMBERG_ANALYTICS_CONFIG = Object.freeze({
  apiBase: "",
  flushIntervalMs: 10000,
  activeTickMs: 15000,
  maxQueueSize: 200
});
