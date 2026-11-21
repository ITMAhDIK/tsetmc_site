/* ============================================================================
 * MODULE 01 — 00-API-CLIENT
 * Responsibility: API transport and endpoint error handling.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 1. API Client ---
    const API_BASE = window.location.protocol.startsWith('http') && window.location.port === '8000'
      ? ''
      : 'http://127.0.0.1:8000';

    async function apiFetch(path, options = {}) {
      const res = await fetch(`${API_BASE}${path}`, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `خطای سرور (${res.status})`);
      }
      return res.json();
    }
