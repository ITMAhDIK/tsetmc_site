"""Market-domain read service extracted from the legacy SiteApi."""
from __future__ import annotations

import json
import time

from .errors import ApiError


class MarketService:
    """Read-only market and stock query operations."""

    def __init__(self, *, sidecar, live, require_ready, meta, q_str, q_int,
                 like_or_clause, normalize_fa, norm_date,
                 gregorian_yyyymmdd_to_jalali, fmt_gregorian_date):
        self.sidecar = sidecar
        self.live = live
        self.require_ready = require_ready
        self.meta = meta
        self._q_str = q_str
        self._q_int = q_int
        self._like_or_clause = like_or_clause
        self._normalize_fa = normalize_fa
        self._norm_date = norm_date
        self._gregorian_yyyymmdd_to_jalali = gregorian_yyyymmdd_to_jalali
        self._fmt_gregorian_date = fmt_gregorian_date
        self._overview_cache = {"at": 0.0, "payload": None}

    def resolve_symbol(self, sc, symbol):
        row = self._resolve_symbol_row(sc, symbol)
        if row is None:
            raise ApiError(404, "نماد یافت نشد: %s" % symbol)
        return row

    # -- 1. Market dashboard ----------------------------------------------
    def market_overview(self):
        now = time.time()
        if (self._overview_cache["payload"] is not None
                and now - self._overview_cache["at"] < 300):
            return self._overview_cache["payload"]
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            total = sc.execute("SELECT COUNT(*) FROM site_watch").fetchone()[0]
            by_market = dict(sc.execute(
                "SELECT market, COUNT(*) FROM site_watch GROUP BY market"
                ).fetchall())
            earliest = self.meta(sc, "earliest_date")
            latest = self.meta(sc, "latest_date")
            holdings_count = self.meta(sc, "holdings_count")
            last_ts = self.meta(sc, "last_fetch_ts")
            # Dashboard panels are precomputed by the background refresh and
            # stored as JSON in site_meta, so this endpoint never touches
            # the live DB (its full-table scans would take minutes).
            top_raw = self.meta(sc, "top_changes")
            recent_raw = self.meta(sc, "recent_holders")
            top_date = self.meta(sc, "top_changes_date_jalali")
        finally:
            sc.close()
        top_changes = json.loads(top_raw) if top_raw else []
        recent = json.loads(recent_raw) if recent_raw else []
        summary = {
            "total_companies": total,
            "tse_companies": by_market.get("TSE", 0),
            "ifb_companies": by_market.get("IFB", 0),
            "unknown_market_companies": by_market.get("UNKNOWN", 0),
            "earliest_date_jalali":
                self._gregorian_yyyymmdd_to_jalali(earliest) if earliest else None,
            "latest_date_jalali":
                self._gregorian_yyyymmdd_to_jalali(latest) if latest else None,
            "total_holdings_rows": int(holdings_count) if holdings_count else 0,
            "last_updated_at": last_ts,
        }
        payload = {"summary": summary,
                   "top_ownership_changes": top_changes,
                   "top_changes_date_jalali": top_date,
                   "recently_observed_shareholders": recent}
        self._overview_cache = {"at": now, "payload": payload}
        return payload

    # -- 2. Market watch ---------------------------------------------------
    def market_watch(self, params):
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            q = self._q_str(params, "q").strip()
            market = self._q_str(params, "market", "ALL")
            sort_by = self._q_str(params, "sort_by", "disclosed_pct")
            sort_dir = self._q_str(params, "sort_dir", "DESC").upper()
            limit = self._q_int(params, "limit", 30, 1, 200)
            offset = self._q_int(params, "offset", 0, 0, 1000000)
            sort_map = {
                "symbol": "symbol",
                "total_shares": "total_company_shares",
                "disclosed_count":
                    "resolved_shareholder_count",
                "disclosed_pct":
                    "resolved_ownership_percent_sum",
                "latest_date": "latest_date",
                "market_value": "market_value",
            }
            col = sort_map.get(sort_by, "resolved_ownership_percent_sum")
            direction = "DESC" if sort_dir != "ASC" else "ASC"
            where = []
            args = []
            if market in ("TSE", "IFB", "UNKNOWN"):
                where.append("market = ?")
                args.append(market)
            if q:
                frag, fargs = self._like_or_clause(
                    ["symbol", "company_name"], q)
                where.append(frag)
                args.extend(fargs)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = sc.execute(
                "SELECT COUNT(*) FROM site_watch " + clause, args).fetchone()[0]
            rows = sc.execute(
                "SELECT symbol, company_name, market, total_company_shares, "
                "  disclosed_shareholder_count, disclosed_ownership_percent_sum, "
                "  resolved_shareholder_count, resolved_ownership_percent_sum, "
                "  resolved_unresolved_count, resolved_status, "
                "  resolved_status_note, "
                "  raw_ownership_percent_sum, latest_price, latest_price_date, "
                "  market_value, total_shareholder_count, "
                "  latest_date_jalali FROM site_watch " + clause +
                " ORDER BY %s %s, symbol ASC LIMIT ? OFFSET ?" % (col, direction),
                args + [limit, offset]).fetchall()
            return {"total": total, "rows": [dict(r) for r in rows]}
        finally:
            sc.close()

    # -- 3. Stock search / detail ------------------------------------------
    def stocks_search(self, params):
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            q = self._q_str(params, "q").strip()
            limit = self._q_int(params, "limit", 8, 1, 50)
            if not q:
                return {"results": []}
            frag, fargs = self._like_or_clause(["symbol", "company_name"], q)
            rows = sc.execute(
                "SELECT symbol, company_name, market FROM site_watch "
                "WHERE " + frag + " "
                "ORDER BY symbol LIMIT ?",
                fargs + [limit]).fetchall()
            return {"results": [dict(r) for r in rows]}
        finally:
            sc.close()

    def _resolve_symbol_row(self, sc, symbol):
        """Exact match, else the same symbol with Persian/Arabic ي/ك
        swapped (the DB stores whichever form TSETMC returned)."""
        row = sc.execute(
            "SELECT * FROM site_watch WHERE symbol=?", (symbol,)).fetchone()
        if row is not None:
            return row
        norm = self._normalize_fa(symbol)
        for r in sc.execute("SELECT * FROM site_watch").fetchall():
            if self._normalize_fa(r["symbol"]) == norm:
                return r
        return None

    def _company_daily(self, live, ins_code):
        return live.execute(
            "SELECT date, disclosed_shareholder_count, "
            "  disclosed_ownership_percent_sum FROM company_daily "
            "WHERE ins_code=? ORDER BY date ASC", (ins_code,)).fetchall()

    def stock_detail(self, symbol):
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
            dedup = {r["date"]: dict(r) for r in sc.execute(
                "SELECT date, resolved_pct, resolved_shares, "
                "  resolved_count, raw_pct, raw_count, raw_shares, "
                "  unquantified_count, unresolved_count, unresolved_shares, "
                "  total_shares, status, status_note "
                "FROM site_company_daily_dedup "
                "WHERE ins_code=?", (w["ins_code"],)).fetchall()}
        finally:
            sc.close()
        live = self.live()
        try:
            comp = live.execute(
                "SELECT cisin, total_shares_current FROM companies "
                "WHERE ins_code=?", (w["ins_code"],)).fetchone()
            daily = self._company_daily(live, w["ins_code"])
        finally:
            live.close()
        # The history chart and table show the IDENTITY-RESOLVED series:
        # the pipeline stores the same holder under several id schemes,
        # so the raw company_daily sum can exceed 100%. The raw values
        # ride along for the tooltip, so the correction is visible, not
        # silent. Days whose identity could not be fully reconciled carry
        # status 'review' (or 'unavailable' when no same-date total
        # exists) with an explanatory note; the percentage shown is the
        # resolved total and is never clamped.
        history = []
        for r in daily:
            d = dedup.get(self._norm_date(r["date"]))
            history.append({
                "date_jalali": self._gregorian_yyyymmdd_to_jalali(r["date"]),
                "disclosed_shareholder_count":
                    d["resolved_count"] if d else None,
                "disclosed_ownership_percent_sum":
                    d["resolved_pct"] if d else None,
                "raw_ownership_percent_sum":
                    d["raw_pct"] if d else
                    r["disclosed_ownership_percent_sum"],
                "raw_shareholder_count":
                    d["raw_count"] if d else
                    r["disclosed_shareholder_count"],
                "resolved_shares": d["resolved_shares"] if d else None,
                "resolved": d is not None,
                "unquantified_count":
                    d["unquantified_count"] if d else None,
                "unresolved_count":
                    d["unresolved_count"] if d else None,
                "unresolved_shares":
                    d["unresolved_shares"] if d else None,
                "total_shares": d["total_shares"] if d else None,
                "status": d["status"] if d else None,
                "status_note": d["status_note"] if d else None,
            })
        return {
            "instrument": {
                "symbol": w["symbol"],
                "company_name": w["company_name"],
                "market": w["market"],
                "ins_code": w["ins_code"],
                "cisin": comp["cisin"] if comp else None,
                "current_issued_shares":
                    comp["total_shares_current"] if comp else None,
                "first_holding_date_jalali": w["first_date_jalali"],
                "latest_holding_date_jalali": w["latest_date_jalali"],
            },
            "latest_snapshot": {
                "total_company_shares": w["total_company_shares"],
                "disclosed_shareholder_count":
                    w["resolved_shareholder_count"],
                "raw_disclosed_shareholder_count":
                    w["disclosed_shareholder_count"],
                "total_shareholder_count": w["total_shareholder_count"],
                "total_shareholder_count_note": w["total_shareholder_count_note"],
                "disclosed_ownership_percent_sum":
                    w["resolved_ownership_percent_sum"],
                "raw_ownership_percent_sum":
                    w["raw_ownership_percent_sum"]
                    if w["raw_ownership_percent_sum"] is not None
                    else w["disclosed_ownership_percent_sum"],
                "unresolved_count": w["resolved_unresolved_count"],
                "status": w["resolved_status"],
                "status_note": w["resolved_status_note"],
                "data_completeness": w["data_completeness"],
                "latest_price": w["latest_price"],
                "latest_price_date": w["latest_price_date"],
                "market_value": w["market_value"],
            },
            "company_history": history,
            "available_dates": [h["date_jalali"] for h in reversed(history)],
        }

    def stock_prices(self, symbol):
        """Daily OHLCV from the pipeline's trading_days (ClosingPrice
        daily list). The dashboard used to claim no candlestick data
        exists; it does -- open=p_first, high=p_max, low=p_min,
        close=p_closing, volume=volume. Rows with no close are skipped."""
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
        finally:
            sc.close()
        live = self.live()
        try:
            rows = live.execute(
                "SELECT date, p_first, p_max, p_min, p_closing, volume "
                "FROM trading_days WHERE ins_code=? AND p_closing IS NOT NULL "
                "ORDER BY date ASC", (w["ins_code"],)).fetchall()
        finally:
            live.close()
        out = []
        for r in rows:
            o = r["p_first"] if r["p_first"] is not None else r["p_closing"]
            out.append({
                "time": self._fmt_gregorian_date(r["date"]),
                "date_jalali": self._gregorian_yyyymmdd_to_jalali(r["date"]),
                "open": o, "high": r["p_max"] or r["p_closing"],
                "low": r["p_min"] or r["p_closing"],
                "close": r["p_closing"],
                "volume": r["volume"],
            })
        return {"prices": out}

