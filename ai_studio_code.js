/*
 * LEGACY SNAPSHOT — this file is not loaded by ai_studio_code.html.
 * The active dashboard is implemented in the inline Babel script in that HTML file.
 * This copy is truncated and must not be treated as the authoritative API client or formatter source.
 */
const { useState, useEffect, useMemo, useRef, useCallback } = React;

// --- 1. API Client ---
// REVIEW NOTE (P3-H02): this duplicate base-URL heuristic has the same remote-deployment
// limitation as the active HTML copy; keep any future migration changes synchronised.
const API_BASE =
  window.location.protocol.startsWith('http') && window.location.port === '8000'
    ? ''
    : 'http://127.0.0.1:8000';

/**
 * Send a JSON request to the configured API base and throw on a non-success status.
 * This legacy helper is retained only as a historical snapshot.
 */
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

// --- 2. Persian Formatters ---
const FA_DIGITS = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];
const toFa = (v) =>
  v === null || v === undefined ? 'نامشخص' : String(v).replace(/\d/g, (d) => FA_DIGITS[Number(d)]);
const fmtNum = (n) =>
  n === null || n === undefined ? 'نامشخص' : toFa(new Intl.NumberFormat('en-US').format(Math.round(n)));
const fmtPct = (p, dec = 3) =>
  p === null || p === undefined ? 'نامشخص' : `${toFa(Number(p).toFixed(dec))}٪`;
const fmtMoney = (rial, unit = 'IRR') => {
  if (rial === null || rial === undefined) return 'نامشخص';
  const val = unit === 'IRT' ? rial / 10 : rial;
  const suffix = unit === 'IRT' ? 'تومان' : 'ری