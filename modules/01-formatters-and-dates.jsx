/* ============================================================================
 * MODULE 02 — 01-FORMATTERS-AND-DATES
 * Responsibility: Persian number, currency, Jalali/Gregorian date formatting and shared date-mode state.
 * Load order: after 00-runtime.jsx and in the numeric order used by index HTML.
 * Shared contracts: React hooks and declarations from earlier modules are
 * available in this page. Keep API payloads and business rules unchanged here.
 * Maintenance: add focused comments beside non-obvious calculations, state
 * transitions, data provenance assumptions, and event handlers.
 * ========================================================================== */
// --- 2. Persian Formatters ---
    const FA_DIGITS = ['۰','۱','۲','۳','۴','۵','۶','۷','۸','۹'];
    const toFa = (v) => (v === null || v === undefined ? 'نامشخص' : String(v).replace(/\d/g, d => FA_DIGITS[Number(d)]));
    const fmtNum = (n) => (n === null || n === undefined ? '—' : toFa(new Intl.NumberFormat('en-US').format(Math.round(n))));
    const fmtPct = (p, dec = 3) => (p === null || p === undefined ? '—' : `${toFa(Number(p).toFixed(dec))}٪`);
    // همت = هزار میلیارد تومان (1e12 Toman)؛ همر = هزار میلیارد ریال (1e12 Rial).
    const fmtMoney = (rial, unit = 'IRR') => {
      if (rial === null || rial === undefined) return '—';
      const val = unit === 'IRT' ? rial / 10 : rial;
      const suffix = unit === 'IRT' ? 'تومان' : 'ریال';
      if (Math.abs(val) >= 1e12) return `${toFa((val / 1e12).toFixed(2))} ${unit === 'IRT' ? 'همت' : 'همر'} (${suffix})`;
      if (Math.abs(val) >= 1e9) return `${toFa((val / 1e9).toFixed(2))} میلیارد ${suffix}`;
      return `${fmtNum(val)} ${suffix}`;
    };

    // --- 2b. Jalali <-> Gregorian (jalaali-js algorithm, 33-year cycle;
    // verified in the VM against 10 known pairs + a 7-year round-trip sweep).
    // div/mod truncate toward zero (NOT Math.floor) -- that matters for
    // negative intermediates like (gm-8)/6.
    const _div = (a, b) => ~~(a / b);
    const _mod = (a, b) => a - _div(a, b) * b;
    function _jalCal(jy) {
      const breaks = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178];
      const bl = breaks.length;
      const gy = jy + 621;
      let leapJ = -14, jp = breaks[0], jump = 0;
      for (let i = 1; i < bl; i++) {
        const jm = breaks[i]; jump = jm - jp;
        if (jy < jm) break;
        leapJ += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4);
        jp = jm;
      }
      let n = jy - jp;
      leapJ += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4);
      if (_mod(jump, 33) === 4 && jump - n === 4) leapJ += 1;
      const leapG = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150;
      const march = 20 + leapJ - leapG;
      if (jump - n < 6) n = n - jump + _div(jump + 4, 33) * 33;
      let leap = _mod(_mod(n + 1, 33) - 1, 4);
      if (leap === -1) leap = 4;
      return { leap, gy, march };
    }
    function _gregorianToJdn(gy, gm, gd) {
      let d = _div((gy + _div(gm - 8, 6) + 100100) * 1461, 4)
        + _div(153 * _mod(gm + 9, 12) + 2, 5) + gd - 34840408;
      d = d - _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4) + 752;
      return d;
    }
    function _jdnToGregorian(jdn) {
      let j = 4 * jdn + 139361631;
      j = j + _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908;
      const i = _div(_mod(j, 1461), 4) * 5 + 308;
      const gd = _div(_mod(i, 153), 5) + 1;
      const gm = _mod(_div(i, 153), 12) + 1;
      const gy = _div(j, 1461) - 100100 + _div(8 - gm, 6);
      return { gy, gm, gd };
    }
    function _jalaliToJdn(jy, jm, jd) {
      const r = _jalCal(jy);
      return _gregorianToJdn(r.gy, 3, r.march) + (jm - 1) * 31 - _div(jm - 1, 7) * (jm - 7) + jd - 1;
    }
    function _jdnToJalali(jdn) {
      const gy = _jdnToGregorian(jdn).gy;
      let jy = gy - 621;
      const r = _jalCal(jy);
      const jdn1f = _gregorianToJdn(gy, 3, r.march);
      let jd, jm, k = jdn - jdn1f;
      if (k >= 0) {
        if (k <= 185) { jm = 1 + _div(k, 31); jd = _mod(k, 31) + 1; return { jy, jm, jd }; }
        k -= 186;
      } else {
        jy -= 1; k += 179;
        if (r.leap === 1) k += 1;
      }
      jm = 7 + _div(k, 30); jd = _mod(k, 30) + 1;
      return { jy, jm, jd };
    }
    const _toJalaali = (gy, gm, gd) => _jdnToJalali(_gregorianToJdn(gy, gm, gd));
    const _toGregorian = (jy, jm, jd) => _jdnToGregorian(_jalaliToJdn(jy, jm, jd));
    const _pad2 = (n) => String(n).padStart(2, '0');

    // Global date mode ('jalali' | 'gregorian'), set by App. fmtDate reads
    // it at render time, so toggling re-renders every date in the UI.
    let DATE_MODE = 'jalali';
    const setDateModeGlobal = (m) => { DATE_MODE = m === 'gregorian' ? 'gregorian' : 'jalali'; };
    // Jalali input ('1405-08-23' or '1405/08/23') -> display per DATE_MODE.
    const fmtDate = (jalaliStr) => {
      if (jalaliStr === null || jalaliStr === undefined || jalaliStr === '') return '—';
      const s = String(jalaliStr).replace(/\//g, '-').slice(0, 10);
      const parts = s.split('-').map(Number);
      if (parts.length !== 3 || parts.some(isNaN)) return toFa(s);
      if (DATE_MODE === 'gregorian') {
        const g = _toGregorian(parts[0], parts[1], parts[2]);
        return toFa(`${g.gy}-${_pad2(g.gm)}-${_pad2(g.gd)}`);
      }
      return toFa(`${parts[0]}/${_pad2(parts[1])}/${_pad2(parts[2])}`);
    };
    // Gregorian input ('2024-01-10') -> display per DATE_MODE.
    const fmtGregorianDate = (gregStr) => {
      if (gregStr === null || gregStr === undefined || gregStr === '') return '—';
      const s = String(gregStr).replace(/\//g, '-').slice(0, 10);
      const parts = s.split('-').map(Number);
      if (parts.length !== 3 || parts.some(isNaN)) return toFa(s);
      if (DATE_MODE === 'gregorian') return toFa(s);
      const j = _toJalaali(parts[0], parts[1], parts[2]);
      return toFa(`${j.jy}/${_pad2(j.jm)}/${_pad2(j.jd)}`);
    };
    // UTC ISO timestamp -> Tehran wall time, 'در تاریخ {date} ساعت {HH:MM}'.
    const fmtDateTime = (iso) => {
      if (!iso) return '—';
      const d = new Date(iso);
      if (isNaN(d.getTime())) return '—';
      const tehran = new Date(d.getTime() + 3.5 * 3600 * 1000);
      const ds = `${tehran.getUTCFullYear()}-${_pad2(tehran.getUTCMonth() + 1)}-${_pad2(tehran.getUTCDate())}`;
      const ts = `${_pad2(tehran.getUTCHours())}:${_pad2(tehran.getUTCMinutes())}`;
      return `در تاریخ ${fmtGregorianDate(ds)} ساعت ${toFa(ts)}`;
    };
