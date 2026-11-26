"""Ownership read service: shareholder snapshots, identity resolution and history."""
from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation

from .errors import ApiError

def resolve_date_rows(live, ins_code, date_g, rows):
    """Group one (ins_code, date)'s holdings rows into resolved
    holders. Same rules as refresh step 8, applied to one day's rows
    in Python: (1) the pipeline's xref canonical (verified /
    inferred only); (2) provable-majority twins among the unlinked
    rows -- same (date, shares, registry name), exactly one candidate
    on each side, shares a strict majority of the same-date issued
    total; (3) everything else stays a separate holder, and groups
    sharing (shares, name) with another group are flagged
    unresolved. Names are a consistency check only, never identity.
    Returns groups ordered by shares desc; each group carries a
    stable backend-supplied group_key the frontend groups by."""
    rank = {"verified_same_source_row": 4,
            "inferred_exact_match": 3,
            "source_id": 2,
            "unlinked_record_id": 1,
            "ambiguous_not_linked": 1}
    if not rows:
        return []
    pairs = {(r["id_scheme"], r["shareholder_id"]) for r in rows}
    ph = ",".join("(?,?)" for _ in pairs)
    args = [v for p in pairs for v in p]
    canon = {}
    for r in live.execute(
            "SELECT source_scheme, source_id, canonical_scheme, "
            "  canonical_id, status FROM shareholder_id_xref "
            "WHERE ins_code=? AND (source_scheme, source_id) IN (%s)"
            % ph, [ins_code] + args).fetchall():
        if r["status"] in ("verified_same_source_row",
                           "inferred_exact_match"):
            canon[(r["source_scheme"], r["source_id"])] = (
                r["canonical_scheme"], r["canonical_id"], r["status"])
    tot = live.execute(
        "SELECT total_company_shares FROM company_daily "
        "WHERE ins_code=? AND date=?", (ins_code, date_g)).fetchone()
    total = tot["total_company_shares"] if tot else None
    if total is None or total <= 0:
        total = live.execute(
            "SELECT MAX(total_company_shares) "
            "FROM shareholder_holdings WHERE ins_code=? AND date=?",
            (ins_code, date_g)).fetchone()[0]
    # (1) group by canonical identity.
    groups = {}
    # Define the representative key once per date, not once per source row.
    rep_fields = (
        "shareholder_name", "shareholder_type", "shares_change",
        "price", "market_value", "valuation_status", "entry_status",
        "ownership_percent_reported", "ownership_percent_calculated")
    numeric_rep_fields = {
        "shares_change", "price", "market_value",
        "ownership_percent_reported", "ownership_percent_calculated",
    }

    def text_rep_key(value):
        # Keep NULL distinct from an empty string, with a stable order.
        return (0, "") if value is None else (1, str(value))

    def numeric_rep_key(value):
        # Compare numeric source values numerically (not as strings:
        # lexicographic ordering would put "9" above "10"). Decimal
        # avoids binary-float ordering surprises and accepts numeric
        # strings from SQLite/source adapters. Malformed/non-finite
        # values remain deterministic and sort below valid numbers.
        if value is None:
            return (0, "")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return (1, str(value))
        if not number.is_finite():
            return (1, str(value))
        return (2, number)

    for r in rows:
        link = canon.get((r["id_scheme"], r["shareholder_id"]))
        key = (link[0], link[1]) if link else (
            r["id_scheme"], r["shareholder_id"])
        basis = None
        if link:
            basis = ("verified" if link[2] == "verified_same_source_row"
                     else "inferred")
        g = groups.get(key)
        if g is None:
            g = {"group_key": "%s:%s" % key, "members": [],
                 "shares": None, "name": None, "name_key": "",
                 "shareholder_type": None, "identity_status": None,
                 "basis": basis or "single", "unresolved": False,
                 "total_shares": total, "rep": None,
                 "rep_key": None}
            groups[key] = g
        g["members"].append({"id_scheme": r["id_scheme"],
                             "shareholder_id": r["shareholder_id"]})
        if r["shares"] is not None and (
                g["shares"] is None or r["shares"] > g["shares"]):
            g["shares"] = r["shares"]
        # Representative row: the best identity tier wins. Within
        # that tier, name_key is the SQL-compatible primary tie-break
        # (D1); a stable tuple of the remaining display fields resolves
        # equal-name ties so endpoint output never depends on SQL row
        # order. This final tie-break is deterministic, not evidence
        # that conflicting source values are correct.
        rep_key = (str(r.get("name_key") or ""),) + tuple(
            numeric_rep_key(r.get(field))
            if field in numeric_rep_fields
            else text_rep_key(r.get(field))
            for field in rep_fields)
        if g["rep"] is None:
            take_rep = True
        else:
            r_rank = rank.get(r["identity_status"], 0)
            g_rank = rank.get(g["identity_status"], 0)
            take_rep = (r_rank > g_rank or
                        (r_rank == g_rank and
                         rep_key > g["rep_key"]))
        if take_rep:
            g["identity_status"] = r["identity_status"]
            g["name"] = r["shareholder_name"]
            g["name_key"] = r["name_key"] or ""
            g["shareholder_type"] = r["shareholder_type"]
            g["rep"] = r
            g["rep_key"] = rep_key
        if basis and g["basis"] == "single":
            g["basis"] = basis
    # (2) provable-majority twins among the unlinked single-row
    # groups: bucket the candidates by shares (not by name -- the
    # name check runs after the exactly-one check, as in the
    # portfolio twin rule), require exactly one ondate + one share
    # candidate, equal registry names, strict majority of the
    # same-date total. Twins collapse to the share_id side.
    cands = [g for g in groups.values()
             if len(g["members"]) == 1 and g["shares"] is not None
             and g["members"][0]["id_scheme"] in (
                 "ondate_share_id", "share_id")
             and g["group_key"] == "%s:%s" % (
                 g["members"][0]["id_scheme"],
                 g["members"][0]["shareholder_id"])]
    by_shares = {}
    for g in cands:
        by_shares.setdefault(g["shares"], []).append(g)
    merged = set()
    for shares, lst in by_shares.items():
        ondates = [g for g in lst
                   if g["members"][0]["id_scheme"] == "ondate_share_id"]
        share_ids = [g for g in lst
                     if g["members"][0]["id_scheme"] == "share_id"]
        if len(ondates) != 1 or len(share_ids) != 1:
            continue
        o, s = ondates[0], share_ids[0]
        if o["name_key"] != s["name_key"]:
            continue
        if not total or shares * 2 <= total:
            continue
        s["members"] = s["members"] + o["members"]
        s["basis"] = "majority_twin"
        merged.add(o["group_key"])
    # (3) unresolved: distinct groups sharing (shares, name).
    by_sn = {}
    for g in groups.values():
        if g["group_key"] in merged:
            continue
        if g["shares"] is None:
            continue
        by_sn.setdefault((g["shares"], g["name_key"]), []).append(g)
    for lst in by_sn.values():
        if len(lst) > 1:
            for g in lst:
                g["unresolved"] = True
    out = []
    for g in groups.values():
        if g["group_key"] in merged:
            continue
        pct = None
        if g["shares"] is not None and total:
            pct = round(g["shares"] * 100.0 / total, 6)
        rep = g["rep"]
        out.append({
            "group_key": g["group_key"],
            "members": g["members"],
            "merged_count": len(g["members"]),
            "shareholder_name": g["name"] or "نامشخص",
            "shareholder_type": g["shareholder_type"],
            "shares": g["shares"],
            "ownership_percent": pct,
            "identity_status": g["identity_status"],
            "basis": g["basis"],
            "unresolved": g["unresolved"],
            "total_shares": total,
            # Display fields from the representative (best-tier)
            # member row; the change/value of a merged group is
            # that member's, and the group's pct is the same-date
            # computed one above.
            "shares_change": rep["shares_change"] if rep else None,
            "price": rep["price"] if rep else None,
            "market_value": rep["market_value"] if rep else None,
            "valuation_status": (rep["valuation_status"]
                                 if rep else None),
            "entry_status": rep["entry_status"] if rep else None,
            "ownership_percent_reported": (
                rep["ownership_percent_reported"] if rep else None),
        })
    out.sort(key=lambda g: (g["shares"] is None, -(g["shares"] or 0)))
    return out




class OwnershipService:
    """Read-only ownership operations over source rows and Sidecar projections."""

    def __init__(self, *, sidecar, live, sidecar_path, require_ready,
                 resolve_symbol, company_daily, majority_twin_map,
                 q_str, q_int, like_or_clause, jalali_to_gregorian_date,
                 gregorian_yyyymmdd_to_jalali, fmt_gregorian_date,
                 fmt_jalali_date, norm_date, latest_close):
        self.sidecar = sidecar
        self.live = live
        self.sidecar_path = sidecar_path
        self.require_ready = require_ready
        self.resolve_symbol = resolve_symbol
        self._company_daily = company_daily
        self._majority_twin_map = majority_twin_map
        self._q_str = q_str
        self._q_int = q_int
        self._like_or_clause = like_or_clause
        self._jalali_to_gregorian_date = jalali_to_gregorian_date
        self._gregorian_yyyymmdd_to_jalali = gregorian_yyyymmdd_to_jalali
        self._fmt_gregorian_date = fmt_gregorian_date
        self._fmt_jalali_date = fmt_jalali_date
        self._norm_date = norm_date
        self._latest_close = latest_close

    def stock_shareholders(self, symbol, params):
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
        finally:
            sc.close()
        date_jalali = self._q_str(params, "date_jalali").strip()
        if date_jalali:
            date_g = self._jalali_to_gregorian_date(date_jalali)
            if not date_g:
                raise ApiError(400, "تاریخ جلالی نامعتبر است: %s" % date_jalali)
        else:
            date_g = w["latest_date"]
            date_jalali = w["latest_date_jalali"]
        sort_by = self._q_str(params, "sort_by", "shares")
        limit = self._q_int(params, "limit", 100, 1, 500)
        order = ("ABS(h.shares_change) DESC" if sort_by == "change"
                 else "h.shares DESC")
        live = self.live()
        # All rows for the date (no LIMIT here): the resolved groups
        # below must cover every holder on this date, or their totals
        # would undercount. Per-date row counts are small (the latest
        # snapshot has ~5.5k rows across all 608 symbols).
        try:
            exclusion_clause = (
                " AND NOT EXISTS ("
                "SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
                "WHERE x.ins_code=h.ins_code AND x.date=h.date "
                "AND x.id_scheme=h.id_scheme "
                "AND x.shareholder_id=h.shareholder_id)")
            total = live.execute(
                "SELECT COUNT(*) FROM shareholder_holdings h "
                "WHERE h.ins_code=? AND h.date=?" + exclusion_clause,
                (w["ins_code"], date_g)).fetchone()[0]
            rows = live.execute(
                "SELECT h.id_scheme AS id_scheme, h.shareholder_id AS shareholder_id, "
                "  s.name AS shareholder_name, "
                "  COALESCE(s.name_normalized, s.name, '') AS name_key, "
                "  s.shareholder_type AS shareholder_type, "
                "  h.shares AS shares, h.ownership_percent AS ownership_percent_reported, "
                "  h.ownership_percent_calc AS ownership_percent_calculated, "
                "  h.shares_change AS shares_change, h.market_value AS market_value, "
                "  h.price AS price, h.valuation_status AS valuation_status, "
                "  h.identity_status AS identity_status, "
                "  h.entry_status AS entry_status "
                "FROM shareholder_holdings h "
                "LEFT JOIN shareholders s "
                "  ON s.id_scheme = h.id_scheme "
                " AND s.shareholder_id = h.shareholder_id "
                "WHERE h.ins_code=? AND h.date=? " + exclusion_clause +
                " ORDER BY %s" % order,
                (w["ins_code"], date_g)).fetchall()
            groups = resolve_date_rows(
                live, w["ins_code"], date_g, [dict(r) for r in rows])
        finally:
            live.close()
        out = []
        for r in rows[:limit]:
            d = dict(r)
            d["shareholder_name"] = d["shareholder_name"] or "نامشخص"
            out.append(d)
        resolved_shares = sum(g["shares"] or 0 for g in groups)
        # resolved_shareholder_count counts QUANTIFIED holders only, so
        # it agrees with the precomputed daily table (step 8) and with
        # market-watch / stock-detail; holders without a share count are
        # reported separately and never silently dropped.
        quantified = [g for g in groups if g["shares"] is not None]
        resolved_pct = None
        total_shares = groups[0]["total_shares"] if groups else None
        if total_shares:
            resolved_pct = round(resolved_shares * 100.0 / total_shares, 6)
        unresolved_n = sum(1 for g in groups if g["unresolved"])
        if total_shares is None or total_shares <= 0:
            status = "unavailable"
            note = ("کل سهام شرکت برای این تاریخ ثبت نشده؛ درصد "
                    "محاسبه نشد.")
        elif unresolved_n:
            status = "review"
            note = ("رکوردهای تکراری حل‌نشده با سهام و نام یکسان وجود "
                    "دارد؛ جمع ممکن است مضاعف باشد.")
        elif resolved_pct is not None and resolved_pct > 100:
            # A resolved total over 100% is never silently 'ok': the
            # records are distinct by every rule we have, yet they sum
            # past the issued shares, so the day needs reconciliation.
            status = "review"
            note = ("جمع مالکیت حل‌شده بیش از ۱۰۰٪ است؛ "
                    "نیاز به بازبینی دارد.")
        else:
            status = "ok"
            note = None
        return {"total": total, "rows": out, "date_jalali": date_jalali,
                "groups": groups,
                "resolved_shareholder_count": len(quantified),
                "unquantified_count": len(groups) - len(quantified),
                "resolved_shares": resolved_shares,
                "resolved_ownership_percent_sum": resolved_pct,
                "unresolved_count": unresolved_n,
                "status": status, "status_note": note}


    def stock_ownership_series(self, symbol, params):
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
            dedup = {r["date"]: dict(r) for r in sc.execute(
                "SELECT date, resolved_pct, resolved_shares, "
                "  resolved_count, raw_pct, status, status_note "
                "FROM site_company_daily_dedup WHERE ins_code=?",
                (w["ins_code"],)).fetchall()}
        finally:
            sc.close()
        scheme = self._q_str(params, "id_scheme").strip()
        sh_id = self._q_str(params, "shareholder_id").strip()
        live = self.live()
        try:
            daily = self._company_daily(live, w["ins_code"])
            # Same-date issued-share totals, for recomputing ownership
            # percentages from shares (the stored
            # ownership_percent_calc divides historical shares by the
            # CURRENT total, which Q1 proved wrong for pre-increase
            # dates). company_daily is authoritative per date; the
            # holdings rows' own total is the fallback.
            totals = {r["date"]: r["total_company_shares"] for r in
                      live.execute(
                          "SELECT date, total_company_shares "
                          "FROM company_daily WHERE ins_code=?",
                          (w["ins_code"],)).fetchall()}

            def _pct_on_date(shares, date, row_total=None):
                total = totals.get(date)
                if total is None or total <= 0:
                    total = row_total
                if shares is None or total is None or total <= 0:
                    return None
                return round(shares * 100.0 / total, 6)

            # Company series: the IDENTITY-RESOLVED percent (the raw
            # pipeline sum counts the same holder under several id
            # schemes and can exceed 100%). The shares series is the
            # resolved share count the major holders hold in total --
            # the "shareholder changes" chart: it rises when they buy,
            # falls when they sell.
            company_series = []
            company_shares_series = []
            for r in daily:
                d = dedup.get(self._norm_date(r["date"]))
                pct = d["resolved_pct"] if d else None
                if pct is not None:
                    company_series.append({
                        "time": self._fmt_gregorian_date(r["date"]), "value": pct})
                if d and d["resolved_shares"] is not None:
                    company_shares_series.append({
                        "time": self._fmt_gregorian_date(r["date"]),
                        "value": d["resolved_shares"]})
            holder_series = []
            ev_where = "h.change_type IN ('new','increase','decrease')"
            ev_args = [w["ins_code"]]
            if scheme and sh_id:
                holder_series = [
                    {"time": self._fmt_gregorian_date(r["date"]),
                     "value": _pct_on_date(r["shares"], r["date"],
                                           r["total_company_shares"])}
                    for r in live.execute(
                        "SELECT h.date, h.shares, h.total_company_shares "
                        "FROM shareholder_holdings h "
                        "WHERE h.ins_code=? AND h.id_scheme=? AND h.shareholder_id=? "
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
                        "WHERE x.ins_code=h.ins_code AND x.date=h.date "
                        "AND x.id_scheme=h.id_scheme "
                        "AND x.shareholder_id=h.shareholder_id) "
                        "ORDER BY h.date ASC",
                        (w["ins_code"], scheme, int(sh_id))).fetchall()
                    if _pct_on_date(r["shares"], r["date"],
                                    r["total_company_shares"]) is not None]
                ev_where += " AND h.id_scheme=? AND h.shareholder_id=?"
                ev_args += [scheme, int(sh_id)]
            events = []
            ev_where += (
                " AND NOT EXISTS ("
                "SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
                "WHERE x.ins_code=h.ins_code AND x.date=h.date "
                "AND x.id_scheme=h.id_scheme "
                "AND x.shareholder_id=h.shareholder_id)")
            for r in live.execute(
                "SELECT h.date AS date, h.jalali_date AS date_jalali, "
                "  h.change_type AS change_type, h.shares_change AS shares_change, "
                "  h.shares AS shares, "
                "  h.total_company_shares AS total_company_shares, "
                "  h.market_value AS market_value, "
                "  h.price AS price, "
                "  s.name AS shareholder_name "
                "FROM shareholder_holdings h "
                "LEFT JOIN shareholders s "
                "  ON s.id_scheme = h.id_scheme "
                " AND s.shareholder_id = h.shareholder_id "
                "WHERE h.ins_code=? AND " + ev_where +
                " ORDER BY h.date DESC LIMIT 200", ev_args).fetchall():
                # The event's value on that day: the row's market value,
                # else the change itself priced at that day's price.
                # Holdings rows almost never carry a price on the real
                # DB, so fall back to the latest closing price.
                val = r["market_value"]
                if val is None and r["shares_change"] is not None \
                        and r["price"] is not None:
                    val = abs(r["shares_change"]) * r["price"]
                if val is None and r["shares_change"] is not None:
                    p = self._latest_close(live, w["ins_code"], r["date"])
                    if p is not None:
                        val = abs(r["shares_change"]) * p
                events.append({
                    "date_jalali": self._fmt_jalali_date(r["date_jalali"]),
                    "date_gregorian": self._fmt_gregorian_date(r["date"]),
                    "shareholder_name": r["shareholder_name"] or "نامشخص",
                    "change_type": r["change_type"],
                    "shares_change": r["shares_change"],
                    # Same-date ownership, recomputed from shares (see
                    # _pct_on_date above); None when no same-date total
                    # exists, instead of the stored calc value.
                    "ownership_percent_calculated": _pct_on_date(
                        r["shares"], r["date"], r["total_company_shares"]),
                    "value_rial": val,
                })
        finally:
            live.close()
        return {"company_series": company_series,
                "company_shares_series": company_shares_series,
                "holder_series": holder_series,
                "events": events}


    def _search_name_pairs(self, live, q):
        """(scheme, id) pairs whose stored name matches q (both ي/ك
        spellings). One scan of the narrow names table (~5 s on the
        real DB) -- never the 6.25M-row holder table."""
        frag, fargs = self._like_or_clause(["name"], q)
        return {(r["id_scheme"], r["shareholder_id"]) for r in live.execute(
            "SELECT id_scheme, shareholder_id "
            "FROM sidecar.site_shareholder_names WHERE " + frag +
            " LIMIT 200", fargs).fetchall()}

    def _search_holder_rows(self, live, q, symbol, stype, min_pct, multi,
                            limit, offset):
        """Candidate holder rows for the tab-3 search, without ever
        scanning site_holder_latest (6,257,365 rows on the real DB).
        Returns row dicts in the shape the grouping code expects
        (shareholder_name, shareholder_type, ins_code, symbol,
        id_scheme, shareholder_id, latest_date, shares, market_value,
        latest_ownership_pct, latest_shares_change, link_status,
        identity_verification_status, first_seen, last_seen).

        Measured 2026-11-20 on the real DB: the old single query
        (holder scan + per-row probes into the names table and the
        55 GB live registry) took 68.8 s for q=28082532 -- and the
        pre-existing code measured 71.9 s, so this was not a
        regression, just a never-measured cost. The new paths:
        exact-id lookup 0.00 s, name scan ~5 s, symbol resolve 0.02 s.
        The registry (first/last seen) is joined only for the fetched
        page, batched, never per scanned row."""
        try:
            min_pct_f = float(min_pct) if min_pct else None
        except ValueError:
            min_pct_f = None
        ins_codes = None
        if symbol:
            frag, fargs = self._like_or_clause(["symbol"], symbol)
            ins_codes = {r["ins_code"] for r in live.execute(
                "SELECT ins_code FROM companies WHERE " + frag,
                fargs).fetchall()}
            if not ins_codes:
                return []
        pairs = None
        if q:
            m = re.match(r"^(share_id|ondate_share_id):(\d+)$", q)
            if m:
                pairs = {(m.group(1), int(m.group(2)))}
            elif q.isdigit():
                # Numeric q: EXACT id match only (0.00 s measured).
                # The old substring match (CAST(id AS TEXT) LIKE '%q%')
                # forced the 69 s full scan; nobody pastes a partial id.
                pairs = set()
                for scheme in ("holder_id", "share_id", "ondate_share_id"):
                    r = live.execute(
                        "SELECT 1 FROM shareholders WHERE id_scheme=? "
                        "AND shareholder_id=? LIMIT 1",
                        (scheme, int(q))).fetchone()
                    if r:
                        pairs.add((scheme, int(q)))
                if not pairs:
                    # No exact id: fall back to a name scan (a name
                    # could contain the digits).
                    pairs = self._search_name_pairs(live, q)
            else:
                pairs = self._search_name_pairs(live, q)
            if not pairs:
                return []
        cols = (
            "n.name AS shareholder_name, "
            "n.shareholder_type AS shareholder_type, "
            "h.ins_code AS ins_code, h.symbol AS symbol, "
            "h.id_scheme AS id_scheme, "
            "h.shareholder_id AS shareholder_id, "
            "h.latest_date AS latest_date, h.shares AS shares, "
            "h.market_value AS market_value, "
            "h.ownership_percent_calc AS latest_ownership_pct, "
            "h.shares_change AS latest_shares_change, "
            "h.identity_status AS link_status, "
            "h.identity_status AS identity_verification_status ")
        join_n = (
            "FROM sidecar.site_holder_latest h "
            "LEFT JOIN sidecar.site_shareholder_names n "
            "  ON n.id_scheme = h.id_scheme "
            " AND n.shareholder_id = h.shareholder_id ")
        if pairs is not None:
            ph = ",".join("(?,?)" for _ in pairs)
            qargs = [v for p in pairs for v in p]
            rows = live.execute(
                "SELECT " + cols + join_n +
                "WHERE (h.id_scheme, h.shareholder_id) IN (%s)" % ph,
                qargs).fetchall()
            rows = [dict(r) for r in rows]
        elif ins_codes is not None:
            ph = ",".join("?" for _ in ins_codes)
            rows = live.execute(
                "SELECT " + cols + join_n +
                "WHERE h.ins_code IN (%s)" % ph,
                list(ins_codes)).fetchall()
            rows = [dict(r) for r in rows]
        else:
            # Browse (no q, no symbol): the index-ordered scan serves
            # ORDER BY + LIMIT directly. The registry is joined only
            # for the fetched page, never per scanned row.
            where = []
            args = []
            if stype in ("legal", "individual"):
                where.append("n.shareholder_type = ?")
                args.append(stype)
            if min_pct_f is not None:
                where.append("h.ownership_percent_calc >= ?")
                args.append(min_pct_f)
            if multi:
                where.append(
                    "(SELECT COUNT(DISTINCT ins_code) "
                    "FROM sidecar.site_holder_latest h2 "
                    "WHERE h2.id_scheme = h.id_scheme "
                    "  AND h2.shareholder_id = h.shareholder_id) > 1")
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            rows = live.execute(
                "SELECT s.first_seen_date AS first_seen, "
                "  s.last_seen_date AS last_seen, t.* FROM ("
                "SELECT " + cols + join_n + clause +
                " ORDER BY h.latest_date DESC, "
                "h.ownership_percent_calc DESC "
                "LIMIT ? OFFSET ?) t "
                "LEFT JOIN shareholders s "
                "  ON s.id_scheme = t.id_scheme "
                " AND s.shareholder_id = t.shareholder_id",
                args + [min(limit * 2, 200), offset]).fetchall()
            rows = [dict(r) for r in rows]
            return rows  # already filtered, ordered and sliced in SQL
        # Post-filters on the fetched rows (the browse path applies
        # them in SQL above).
        if ins_codes is not None:
            rows = [r for r in rows if r["ins_code"] in ins_codes]
        if stype in ("legal", "individual"):
            rows = [r for r in rows
                    if r["shareholder_type"] == stype]
        if min_pct_f is not None:
            rows = [r for r in rows
                    if (r["latest_ownership_pct"] or 0) >= min_pct_f]
        if multi and rows:
            # NOTE: counts per (scheme, id), not per person -- a holder
            # whose symbols are split across twin ids (one symbol each)
            # does not match multi_only. Grouping (below) merges twins
            # after the filter.
            pp = {(r["id_scheme"], r["shareholder_id"]) for r in rows}
            ph = ",".join("(?,?)" for _ in pp)
            qargs = [v for p in pp for v in p]
            counts = {}
            for rr in live.execute(
                    "SELECT id_scheme, shareholder_id, "
                    "COUNT(DISTINCT ins_code) AS c "
                    "FROM sidecar.site_holder_latest "
                    "WHERE (id_scheme, shareholder_id) IN (%s) "
                    "GROUP BY id_scheme, shareholder_id" % ph,
                    qargs).fetchall():
                counts[(rr["id_scheme"], rr["shareholder_id"])] = rr["c"]
            rows = [r for r in rows
                    if counts.get((r["id_scheme"],
                                   r["shareholder_id"]), 0) > 1]
        # first/last seen, batched for the surviving pairs.
        pp = {(r["id_scheme"], r["shareholder_id"]) for r in rows}
        seen = {}
        if pp:
            ph = ",".join("(?,?)" for _ in pp)
            qargs = [v for p in pp for v in p]
            for rr in live.execute(
                    "SELECT id_scheme, shareholder_id, first_seen_date, "
                    "last_seen_date FROM shareholders "
                    "WHERE (id_scheme, shareholder_id) IN (%s)" % ph,
                    qargs).fetchall():
                seen[(rr["id_scheme"], rr["shareholder_id"])] = (
                    rr["first_seen_date"], rr["last_seen_date"])
        for r in rows:
            f, l = seen.get((r["id_scheme"], r["shareholder_id"]),
                            (None, None))
            r["first_seen"] = f
            r["last_seen"] = l
        rows.sort(key=lambda r: (r["latest_date"] or "",
                                 r["latest_ownership_pct"] or 0),
                  reverse=True)
        return rows[offset:offset + min(limit * 2, 200)]

    def shareholders_search(self, params):
        sc = self.sidecar()
        try:
            self.require_ready(sc)
        finally:
            sc.close()
        q = self._q_str(params, "q").strip()
        symbol = self._q_str(params, "symbol").strip()
        stype = self._q_str(params, "shareholder_type", "ALL")
        multi = self._q_str(params, "multi_company_only", "false").lower() == "true"
        min_pct = self._q_str(params, "min_ownership").strip()
        limit = self._q_int(params, "limit", 40, 1, 100)
        offset = self._q_int(params, "offset", 0, 0, 1000000)
        live = self.live()
        try:
            path = os.path.abspath(self.sidecar_path).replace("'", "''")
            live.execute("ATTACH '%s' AS sidecar" % path)
            rows = self._search_holder_rows(
                live, q, symbol, stype, min_pct, multi, limit, offset)
            # Group rows by PERSON, not by (holder, symbol): the same
            # investor appears once per symbol and once per id scheme
            # (an ondate twin + its share_id twin). The group key comes
            # from the pipeline's own raw_source_ids (see
            # _linked_identity_ids) or, failing that, from a
            # provable-majority twin (see _majority_twin_map): the
            # share_id member when one exists, else the row's own id.
            # One batched registry lookup for the whole page, never
            # per-row. This deliberately does NOT use the stock page's
            # name-dedup: that would merge ALL masked persons (every
            # 'شخص حقیقی') into one group.
            pairs = {(r["id_scheme"], r["shareholder_id"]) for r in rows}
            raw_map = {}
            if pairs:
                ph = ",".join("(?,?)" for _ in pairs)
                qargs = [v for p in pairs for v in p]
                for rr in live.execute(
                    "SELECT id_scheme, shareholder_id, raw_source_ids "
                    "FROM shareholders "
                    "WHERE (id_scheme, shareholder_id) IN (%s)" % ph,
                    qargs).fetchall():
                    raw_map[(rr["id_scheme"], rr["shareholder_id"])] = \
                        rr["raw_source_ids"]
            twin_map = self._majority_twin_map(
                live, live, rows, tbl="sidecar.site_holder_latest")

            def _group_key(scheme, sh_id):
                members = [(scheme, sh_id)]
                raw = raw_map.get((scheme, sh_id))
                if raw:
                    for part in raw.split(","):
                        part = part.strip()
                        if ":" in part:
                            s2, i2 = part.split(":", 1)
                            try:
                                members.append((s2.strip(), int(i2.strip())))
                            except ValueError:
                                pass
                for s2, i2 in members:
                    if s2 == "share_id":
                        return ("share_id", i2)
                t = twin_map.get((scheme, sh_id))
                if t and t[0] == "share_id":
                    return t
                return (scheme, sh_id)

            groups = {}
            order = []
            for r in rows:
                key = _group_key(r["id_scheme"], r["shareholder_id"])
                g = groups.get(key)
                if g is None:
                    g = {"group_id": "%s:%d" % key,
                         "id_scheme": key[0], "shareholder_id": key[1],
                         "shareholder_name": r["shareholder_name"],
                         "shareholder_type": r["shareholder_type"],
                         "symbols": [], "first_seen": r["first_seen"],
                         "last_seen": r["last_seen"]}
                    groups[key] = g
                    order.append(key)
                # Name/type: prefer the row that IS the group key (the
                # stable share_id record) over a twin's row.
                if (r["id_scheme"], r["shareholder_id"]) == key:
                    if r["shareholder_name"]:
                        g["shareholder_name"] = r["shareholder_name"]
                    if r["shareholder_type"]:
                        g["shareholder_type"] = r["shareholder_type"]
                if r["first_seen"] and (not g["first_seen"]
                                        or r["first_seen"] < g["first_seen"]):
                    g["first_seen"] = r["first_seen"]
                if r["last_seen"] and (not g["last_seen"]
                                       or r["last_seen"] > g["last_seen"]):
                    g["last_seen"] = r["last_seen"]
                g["symbols"].append({
                    "ins_code": r["ins_code"], "symbol": r["symbol"],
                    "id_scheme": r["id_scheme"],
                    "shareholder_id": r["shareholder_id"],
                    "latest_date": r["latest_date"],
                    "shares": r["shares"],
                    "market_value": r["market_value"],
                    "latest_ownership_pct": r["latest_ownership_pct"],
                    "latest_shares_change": r["latest_shares_change"],
                })
        finally:
            live.close()
        out = []
        for key in order[:limit]:
            g = groups[key]
            syms = g.pop("symbols")
            # Dedupe symbols by ins_code: a twin pair on the same symbol
            # is one company. Prefer the group-key id's row.
            by_ins = {}
            for s in syms:
                cur = by_ins.get(s["ins_code"])
                if cur is None:
                    by_ins[s["ins_code"]] = s
                elif (s["id_scheme"], s["shareholder_id"]) == key:
                    by_ins[s["ins_code"]] = s
            syms = list(by_ins.values())
            # Largest position first; the group's headline % / change are
            # that position's (the max-% symbol).
            syms.sort(key=lambda s: (s["latest_ownership_pct"] or 0),
                      reverse=True)
            top = syms[0] if syms else {}
            d = {"group_id": g["group_id"],
                 "id_scheme": g["id_scheme"],
                 "shareholder_id": g["shareholder_id"],
                 "shareholder_name": g["shareholder_name"],
                 "shareholder_type": g["shareholder_type"],
                 "n_companies": len({s["ins_code"] for s in syms}),
                 "symbols": syms,
                 "latest_ownership_pct": top.get("latest_ownership_pct"),
                 "latest_shares_change": top.get("latest_shares_change"),
                 "first_observed_date_jalali": (
                     self._gregorian_yyyymmdd_to_jalali(g["first_seen"])
                     if g.get("first_seen") else None),
                 "last_observed_date_jalali": (
                     self._gregorian_yyyymmdd_to_jalali(g["last_seen"])
                     if g.get("last_seen") else None)}
            out.append(d)
        return {"rows": out}

    def shareholder_observation_history(self, params):
        symbol = self._q_str(params, "symbol").strip()
        scheme = self._q_str(params, "id_scheme").strip()
        sh_id = self._q_str(params, "shareholder_id").strip()
        if not (symbol and scheme and sh_id):
            raise ApiError(400, "پارامترهای symbol و id_scheme و shareholder_id لازم‌اند.")
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
        finally:
            sc.close()
        live = self.live()
        try:
            rows = live.execute(
                "SELECT h.date, h.ownership_percent_calc FROM shareholder_holdings h "
                "WHERE h.ins_code=? AND h.id_scheme=? AND h.shareholder_id=? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
                "  WHERE x.ins_code=h.ins_code AND x.date=h.date "
                "    AND x.id_scheme=h.id_scheme "
                "    AND x.shareholder_id=h.shareholder_id) "
                "ORDER BY h.date ASC",
                (w["ins_code"], scheme, int(sh_id))).fetchall()
        finally:
            live.close()
        return {"history": [
            {"time": self._fmt_gregorian_date(r["date"]),
             "value": r["ownership_percent_calc"]}
            for r in rows if r["ownership_percent_calc"] is not None]}

