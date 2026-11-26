"""Portfolio read service for investor identity and historical positions.

The service is an application-layer boundary over existing read-only source
SQLite rows and the materialized Sidecar history. Identity resolution is
injected from SiteApi because its batched majority-twin map is also shared
with the ownership search service.
"""
from __future__ import annotations

import heapq
import itertools
from datetime import datetime

from .errors import ApiError


def _like(value):
    return "%" + str(value).replace("%", "").replace("_", "") + "%"


class PortfolioService:
    """Investor lookup, linked identity, current positions and dated history."""

    def __init__(
        self,
        *,
        sidecar,
        live,
        site_dir,
        require_ready,
        majority_twin_map,
        q_str,
        q_int,
        norm_date,
        fmt_jalali_date,
        gregorian_yyyymmdd_to_jalali,
        ownership_percent_for_date,
        classify_portfolio_change,
        classify_historical_exit,
        build_portfolio_history_summary,
        load_sector_reference,
        sector_affinity_accumulator_factory,
    ):
        self.sidecar = sidecar
        self.live = live
        self.site_dir = site_dir
        self.require_ready = require_ready
        self._majority_twin_map = majority_twin_map
        self._q_str = q_str
        self._q_int = q_int
        self._norm_date = norm_date
        self._fmt_jalali_date = fmt_jalali_date
        self._gregorian_yyyymmdd_to_jalali = gregorian_yyyymmdd_to_jalali
        self._ownership_percent_for_date = ownership_percent_for_date
        self._classify_portfolio_change = classify_portfolio_change
        self._classify_historical_exit = classify_historical_exit
        self._build_portfolio_history_summary = build_portfolio_history_summary
        self._load_sector_reference = load_sector_reference
        self._sector_affinity_accumulator_factory = sector_affinity_accumulator_factory

    def portfolio_investors(self, params):
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            q = self._q_str(params, "q").strip()
            limit = self._q_int(params, "limit", 30, 1, 100)
            # sort_by=linked (default: most records) or inferred (most
            # exact-match inferred links first -- the investors whose
            # identity is actually evidenced; by record count they are
            # buried, because an inferred holder typically has few rows).
            # Ordering by inferred is served by idx_investors_inferred.
            sort_by = self._q_str(params, "sort_by").strip()
            sort_col = {"inferred": "i.inferred_links_count"}.get(
                sort_by, "i.linked_records_count")
            # Link counts come from the materialised site_investors table
            # (built in the refresh); names from the names table. The old
            # per-request GROUP BY over the whole holder table was far too
            # slow on the real database.
            where = ""
            args = []
            if q:
                frag, fargs = _like_or_clause(["n.name"], q)
                where = ("WHERE " + frag + " "
                         "OR CAST(i.shareholder_id AS TEXT) LIKE ?")
                args = fargs + [_like(q)]
            # No second sort key: canonical_name lives in the joined
            # names table, so it cannot be part of the index on
            # linked_records_count, and ordering by it forced a full
            # sort of all 5.3M investor rows (93 s on the real DB).
            # Ordering by the count alone is served by
            # idx_investors_count in index order.
            rows = sc.execute(
                "SELECT i.id_scheme, i.shareholder_id, "
                "  n.name AS canonical_name, "
                "  i.linked_records_count AS linked_records_count, "
                "  i.verified_links_count AS verified_links_count, "
                "  i.inferred_links_count AS inferred_links_count "
                "FROM site_investors i "
                "LEFT JOIN site_shareholder_names n "
                "  ON n.id_scheme = i.id_scheme "
                " AND n.shareholder_id = i.shareholder_id "
                + where +
                " ORDER BY %s DESC " % sort_col +
                "LIMIT ?", args + [limit]).fetchall()
            return {"investors": [{
                "id": "%s:%s" % (r["id_scheme"], r["shareholder_id"]),
                "canonical_name": r["canonical_name"] or "نامشخص",
                "verified_links_count": r["verified_links_count"] or 0,
                "inferred_links_count": r["inferred_links_count"] or 0,
                "linked_records_count": r["linked_records_count"],
            } for r in rows]}
        finally:
            sc.close()


    def _linked_identity_ids(self, live, sc, scheme, sh_id):
        """(scheme, id) pairs that are the same person as (scheme, sh_id).

        Two sources, both conservative:
        (1) The pipeline's own links: `shareholders.raw_source_ids`
        (collectors/pipeline.py: raw_ids = sources_of(canonical)) -- the
        full member list, written on every member of a merged identity.
        (2) Provable-majority twins (see _majority_twins): same
        (ins_code, latest_date, shares, name) with a strict-majority
        stake, which two distinct holders cannot share.
        Nothing is ever merged on name alone, and a small stake under two
        ids stays two people. If neither source links anything, the
        result is just the requested id (current behaviour).
        """
        def _parse_raw(raw):
            out = set()
            if not raw:
                return out
            for part in raw.split(","):
                part = part.strip()
                if ":" not in part:
                    continue
                s2, i2 = part.split(":", 1)
                try:
                    out.add((s2.strip(), int(i2.strip())))
                except ValueError:
                    continue
            return out

        def _chunks(seq, n=400):
            for i in range(0, len(seq), n):
                yield seq[i:i + n]

        def _raw_closure(seeds):
            # Transitive closure over the pipeline's raw_source_ids
            # links, BATCHED by BFS level: one IN query per chunk of
            # the frontier instead of one point lookup per id. For the
            # 1307560 identity the closure is 9,514 ids; the per-id
            # loop ran ~9,500 cold seeks into the 55 GB registry
            # (45.6 s measured 2026-11-20). The batched version fetches
            # the same raw lists in ~24 queries. raw_source_ids is the
            # FULL member list on every member, so the closure is 1-2
            # levels deep and the result set is identical to the
            # per-id BFS. Each id enters the frontier once, so no id
            # is ever re-queried.
            found = set(seeds)
            frontier = list(seeds)
            while frontier:
                nxt = []
                for part in _chunks(frontier):
                    ph = ",".join("(?,?)" for _ in part)
                    qargs = [v for p in part for v in p]
                    for row in live.execute(
                        "SELECT id_scheme, shareholder_id, "
                        "  raw_source_ids FROM shareholders "
                        "WHERE (id_scheme, shareholder_id) IN (%s)" % ph,
                        qargs).fetchall():
                        for m in _parse_raw(row["raw_source_ids"]):
                            if m not in found:
                                found.add(m)
                                nxt.append(m)
                frontier = nxt
            return found

        ids = _raw_closure([(scheme, sh_id)])
        # Provable-majority twins, BATCHED over all closure ids at once:
        # one _majority_twin_map call (3 batched queries) instead of one
        # _majority_twins per id, which ran a candidate scan plus 2
        # registry probes plus a companies probe per position row of
        # every raw member -- for an investor with hundreds of raw
        # members that was thousands of cold probes into the 55 GB live
        # DB (134 s for one portfolio). One level only: a twin's twin
        # is the requested id itself by symmetry.
        twin_map = {}
        if ids:
            twin_rows = []
            for part in _chunks(list(ids)):
                ph = ",".join("(?,?)" for _ in part)
                qargs = [v for p in part for v in p]
                twin_rows.extend(sc.execute(
                    "SELECT ins_code, latest_date, shares, id_scheme, "
                    "shareholder_id FROM site_holder_latest "
                    "WHERE (id_scheme, shareholder_id) IN (%s) "
                    "AND shares IS NOT NULL" % ph, qargs).fetchall())
            if twin_rows:
                twin_map = self._majority_twin_map(live, sc, twin_rows)
        for cur in list(ids):
            t = twin_map.get(cur)
            if t and t not in ids:
                ids |= _raw_closure([t])
        # Requested id FIRST: _holder_positions prefers the first id's
        # row for a shared symbol, so the investor's own record wins over
        # a twin's.
        return [(scheme, sh_id)] + sorted(ids - {(scheme, sh_id)})

    def _holder_positions(self, live, sc, ids, as_of_dashed):
        """Latest position per symbol for a linked identity (a list of
        (scheme, id) pairs from _linked_identity_ids, requested id
        FIRST), on/before as_of_dashed ('YYYY-MM-DD') or the latest
        overall when as_of_dashed is None.
        Returns [(symbol, row_dict, origin_scheme, origin_id)]: when the
        same symbol appears under several ids of the identity (the twin
        records the same position), the first id in `ids` wins, so the
        requested id's own record is preferred over a twin's."""
        # self.live() attaches the sidecar read-only, so historical
        # position lookups can use the same exclusions as the latest index.
        # Step 1: which (symbol, id) pairs actually exist -- ONE batched
        # query over site_holder_latest, whose idx_holder_scheme
        # (id_scheme, shareholder_id) serves the IN list as point seeks
        # (measured 0.03 s for 736 ids on the real DB). Chunked so the
        # IN list stays well under SQLite's parameter limit even for
        # identities with thousands of raw members.
        id_list = list(ids)

        def _chunks(seq, n=400):
            for i in range(0, len(seq), n):
                yield seq[i:i + n]

        triples = []  # (ins_code, symbol, scheme, sh_id)
        for part in _chunks(id_list):
            ph = ",".join("(?,?)" for _ in part)
            qargs = [v for p in part for v in p]
            triples.extend(sc.execute(
                "SELECT ins_code, symbol, id_scheme, shareholder_id "
                "FROM site_holder_latest "
                "WHERE (id_scheme, shareholder_id) IN (%s)" % ph,
                qargs).fetchall())
        # Step 2: ONE point lookup per SYMBOL, for the winning id only.
        # _holder_positions returns one position per symbol (first id in
        # `ids` order wins), so looking up every (symbol, id) pair is
        # wasted work: for the 1307560 identity the closure grew to
        # 9,514 ids (raw members of CISIN twins) and the per-pair loop
        # ran ~9,500 point lookups into the 55 GB live DB (29.6 s).
        # Instead, ids are tried in `ids` order per symbol and the
        # first one with a row wins -- in the common case (no as_of,
        # or the winner has a row) that is exactly ONE lookup per
        # symbol (159 for that identity instead of ~9,500). Only when
        # an as_of date is set and the winner's rows are all after it
        # does the loop fall through to the next id, exactly as the
        # old per-pair code did. A point lookup with all three
        # autoindex columns as equalities is a single index seek
        # (verified in EXPLAIN QUERY PLAN). The ASOF close rides along
        # as a correlated subquery, so no separate price lookup per row
        # is needed. (The batched alternative -- one IN-list query per
        # symbol -- was measured on the real DB: the row-value IN sits
        # on the TRAILING columns of the autoindex and the planner
        # scans the symbol's rows via idx_holdings_date with a bloom
        # filter instead of point seeks.)
        date_clause = " AND date<=?" if as_of_dashed else ""
        date_args = [as_of_dashed] if as_of_dashed else []
        order = {p: i for i, p in enumerate(id_list)}
        by_symbol = {}
        for ins_code, symbol, scheme, sh_id in triples:
            by_symbol.setdefault(ins_code, [symbol, []])[1].append(
                (scheme, sh_id))
        positions = []
        for ins_code, (symbol, id_pairs) in by_symbol.items():
            id_pairs.sort(key=lambda p: order.get(p, 1 << 30))
            for scheme, sh_id in id_pairs:
                row = live.execute(
                    "SELECT h.*, "
                    "  (SELECT cd.total_company_shares FROM company_daily cd "
                    "   WHERE cd.ins_code=h.ins_code AND cd.date=h.date "
                    "   LIMIT 1) AS same_date_total_company_shares, "
                    "  (SELECT t.p_closing FROM trading_days t "
                    "   WHERE t.ins_code = h.ins_code AND t.date <= h.date "
                    "     AND t.p_closing IS NOT NULL AND t.p_closing > 0 "
                    "   ORDER BY t.date DESC LIMIT 1) AS close_price, "
                    "  (SELECT t.date FROM trading_days t "
                    "   WHERE t.ins_code = h.ins_code AND t.date <= h.date "
                    "     AND t.p_closing IS NOT NULL AND t.p_closing > 0 "
                    "   ORDER BY t.date DESC LIMIT 1) AS close_price_date "
                    "FROM shareholder_holdings h "
                    "WHERE h.ins_code=? AND h.id_scheme=? "
                    "  AND h.shareholder_id=? "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
                    "    WHERE x.ins_code=h.ins_code AND x.date=h.date "
                    "      AND x.id_scheme=h.id_scheme "
                    "      AND x.shareholder_id=h.shareholder_id) %s "
                    "ORDER BY h.date DESC LIMIT 1" % date_clause,
                    (ins_code, scheme, sh_id) + tuple(date_args)).fetchone()
                if row is not None:
                    positions.append((symbol, dict(row), scheme, sh_id))
                    break
        return positions

    def _holder_history_stream(self, live, ids, as_of_dashed=None, ins_code=None):
        """Stream all history through the sidecar identity/date primary-key index."""
        unique_ids = list(dict.fromkeys(ids or []))
        if not unique_ids:
            return iter(())
        streams = []
        for start in range(0, len(unique_ids), 350):
            part = unique_ids[start:start + 350]
            pairs_sql = ",".join("(?,?)" for _ in part)
            args = [value for pair in part for value in pair]
            where = "(h.id_scheme, h.shareholder_id) IN (%s)" % pairs_sql
            if as_of_dashed:
                where += " AND h.date<=?"
                args.append(as_of_dashed)
            if ins_code:
                where += " AND h.ins_code=?"
                args.append(ins_code)
            sql = (
                "SELECT h.*, "
                "CASE WHEN h.price IS NOT NULL AND h.price>0 "
                "AND (h.price_date IS NULL OR h.price_date<=h.date) "
                "THEN h.price ELSE tp.p_closing END AS reference_price, "
                "CASE WHEN h.price IS NOT NULL AND h.price>0 "
                "AND (h.price_date IS NULL OR h.price_date<=h.date) "
                "THEN COALESCE(h.price_date,h.date) ELSE tp.date END AS reference_price_date "
                "FROM sidecar_data.site_holder_history h "
                "LEFT JOIN trading_days tp "
                "ON (h.price IS NULL OR h.price<=0) AND tp.ins_code=h.ins_code "
                "AND tp.date=(SELECT MAX(t.date) FROM trading_days t "
                "WHERE t.ins_code=h.ins_code AND t.date<=h.date "
                "AND t.p_closing IS NOT NULL AND t.p_closing>0) "
                "WHERE " + where +
                " ORDER BY h.date, h.ins_code, h.id_scheme, h.shareholder_id, h.id")
            cursor = live.execute(sql, args)
            def _rows(c=cursor):
                while True:
                    batch = c.fetchmany(1000)
                    if not batch:
                        break
                    for raw in batch:
                        yield dict(raw)
            streams.append(_rows())
        return heapq.merge(*streams, key=lambda row: (
            row["date"], row["ins_code"], row["id_scheme"], row["shareholder_id"]))

    def _iter_portfolio_history_observations(
            self, live, ids, requested_pair, as_of_dashed=None, ins_code=None):
        """Collapse linked identity rows only when same-date quantities agree."""
        priority = {pair: idx for idx, pair in enumerate(ids or [])}
        def rank(row):
            pair = (row["id_scheme"], row["shareholder_id"])
            status_rank = {"verified_same_source_row": 0,
                           "inferred_exact_match": 1}.get(row.get("identity_status"), 2)
            return (0 if pair == requested_pair else 1,
                    priority.get(pair, 1 << 30), status_rank,
                    row["id_scheme"], row["shareholder_id"])
        stream = self._holder_history_stream(
            live, ids, as_of_dashed=as_of_dashed, ins_code=ins_code)
        previous_by_instrument = {}
        for _key, group in itertools.groupby(
                stream, key=lambda row: (row["date"], row["ins_code"])):
            candidates = sorted(list(group), key=rank)
            chosen = dict(candidates[0])
            conflict = len({row.get("shares") for row in candidates}) > 1
            chosen["identity_conflict"] = conflict
            chosen["identity_candidate_count"] = len(candidates)
            chosen["identity_deduplicated_count"] = max(0, len(candidates) - 1)
            chosen["identity_candidate_ids"] = [
                "%s:%s" % (row["id_scheme"], row["shareholder_id"])
                for row in candidates]
            if conflict:
                chosen["shares"] = None
                chosen["shares_change"] = None
                chosen["reference_price"] = None

            # Older source databases may not store explicit previous-share
            # fields or a change-calculation status. Derive these from the
            # prior observation, but never bridge a missing-disclosure gap
            # as if it were a consecutive observation.
            ins_code_key = chosen["ins_code"]
            prior = previous_by_instrument.get(ins_code_key)
            if chosen.get("previous_shares") is None and prior:
                chosen["previous_shares"] = prior.get("last_known_shares")
            if chosen.get("previous_date") is None and prior:
                chosen["previous_date"] = prior.get("last_known_date")
            status = chosen.get("change_calculation_status")
            if not status or status == "legacy_unclassified":
                if chosen.get("shares") is None:
                    status = "not_disclosed_on_date"
                elif prior is None:
                    status = "first_observation_no_previous"
                elif prior.get("last_observation_shares") is None:
                    status = "spans_gap"
                else:
                    status = "consecutive_observations"
                    if chosen.get("shares_change") is None:
                        chosen["shares_change"] = (
                            int(chosen["shares"]) -
                            int(prior["last_observation_shares"]))
                chosen["change_calculation_status"] = status
            if chosen.get("shares_change_percent") is None:
                prev = chosen.get("previous_shares")
                delta = chosen.get("shares_change")
                if prev not in (None, 0) and delta is not None:
                    chosen["shares_change_percent"] = round(
                        100.0 * float(delta) / float(prev), 6)

            chosen["ownership_percent_calculated"] = self._ownership_percent_for_date(
                chosen.get("shares"), chosen.get("total_company_shares"))
            shares, price = chosen.get("shares"), chosen.get("reference_price")
            chosen["estimated_holding_value_rial"] = (
                shares * price if shares is not None and price is not None and price > 0
                else None)
            chosen["change_classification"] = self._classify_portfolio_change(chosen)
            state = previous_by_instrument.setdefault(ins_code_key, {})
            state["last_observation_date"] = chosen.get("date")
            state["last_observation_shares"] = chosen.get("shares")
            if chosen.get("shares") is not None and not conflict:
                state["last_known_date"] = chosen.get("date")
                state["last_known_shares"] = chosen.get("shares")
            yield chosen

    def portfolio_history(self, investor_id, params):
        """Paginated chronological observations for one historical security."""
        try:
            scheme, raw_id = investor_id.split(":", 1)
            sh_id = int(raw_id)
        except (ValueError, AttributeError):
            raise ApiError(400, "شناسه سرمایه‌گذار نامعتبر است.")
        ins_code = self._q_str(params, "ins_code").strip()
        if not ins_code:
            raise ApiError(400, "پارامتر ins_code برای تاریخچه لازم است.")
        as_of = self._q_str(params, "as_of_date").strip()
        as_of_dashed = self._norm_date(as_of) if as_of else None
        if as_of_dashed:
            try:
                datetime.strptime(as_of_dashed, "%Y-%m-%d")
            except ValueError:
                raise ApiError(400, "تاریخ مبنا باید YYYY-MM-DD باشد.")
        limit = self._q_int(params, "limit", 200, 1, 500)
        offset = self._q_int(params, "offset", 0, 0, 100000000)
        live, sc = self.live(), self.sidecar()
        try:
            self.require_ready(sc)
            ids = self._linked_identity_ids(live, sc, scheme, sh_id)
            total, page_rows = 0, []
            for row in self._iter_portfolio_history_observations(
                    live, ids, (scheme, sh_id), as_of_dashed, ins_code):
                if offset <= total < offset + limit:
                    delta, price = row.get("shares_change"), row.get("reference_price")
                    page_rows.append({
                        "symbol": row["symbol"], "date": row["date"],
                        "date_jalali": self._fmt_jalali_date(row.get("jalali_date")),
                        "change_type": row.get("change_type"),
                        "change_classification": row["change_classification"],
                        "shares": row.get("shares"),
                        "previous_date": row.get("previous_date"),
                        "previous_date_jalali": (
                            self._gregorian_yyyymmdd_to_jalali(row["previous_date"])
                            if row.get("previous_date") else None),
                        "previous_shares": row.get("previous_shares"),
                        "shares_change": delta,
                        "shares_change_percent": row.get("shares_change_percent"),
                        "ownership_percent_calculated": row.get("ownership_percent_calculated"),
                        "total_company_shares": row.get("total_company_shares"),
                        "reference_price": price,
                        "reference_price_date": row.get("reference_price_date"),
                        "price_field": (
                            row.get("price_field") or
                            ("trading_days.p_closing" if price is not None else None)),
                        "source_market_value_rial": row.get("market_value"),
                        "previous_close_price": row.get("prev_close_price"),
                        "previous_close_date": row.get("prev_close_date"),
                        "source_endpoint": row.get("source_endpoint"),
                        "reference_price_date_jalali": (
                            self._gregorian_yyyymmdd_to_jalali(row["reference_price_date"])
                            if row.get("reference_price_date") else None),
                        "estimated_holding_value_rial": row.get("estimated_holding_value_rial"),
                        "estimated_change_value_rial": (
                            abs(delta) * price if delta is not None and price is not None
                            and price > 0 else None),
                        "corporate_action": row.get("corporate_action"),
                        "corporate_action_date": row.get("corporate_action_date"),
                        "corporate_action_ratio": row.get("corporate_action_ratio"),
                        "source_change_code": row.get("source_change_code"),
                        "source_change_amount": row.get("source_change_amount"),
                        "gap_trading_days": row.get("gap_trading_days"),
                        "change_calculation_status": row.get("change_calculation_status"),
                        "entry_status": row.get("entry_status"),
                        "valuation_status": row.get("valuation_status"),
                        "data_available": bool(row.get("data_available")),
                        "identity_conflict": row.get("identity_conflict", False),
                        "identity_candidate_ids": row.get("identity_candidate_ids", []),
                        "anomaly_flags": row.get("anomaly_flags"),
                        "note": row.get("note"),
                    })
                total += 1
        finally:
            live.close()
            sc.close()
        return {"investor_id": investor_id, "ins_code": ins_code,
                "total": total, "limit": limit, "offset": offset,
                "rows": page_rows}

    def portfolio_detail(self, investor_id, params):
        try:
            scheme, sh_id = investor_id.split(":", 1)
            sh_id = int(sh_id)
        except (ValueError, AttributeError):
            raise ApiError(400, "شناسه سرمایه‌گذار نامعتبر است.")
        as_of = self._q_str(params, "as_of_date").strip()
        as_of_dashed = self._norm_date(as_of) if as_of else None
        if as_of_dashed:
            try:
                datetime.strptime(as_of_dashed, "%Y-%m-%d")
            except ValueError:
                raise ApiError(400, "تاریخ مبنا باید YYYY-MM-DD باشد.")
        live = self.live()
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            reg = live.execute(
                "SELECT name, identity_status, shareholder_type, "
                "  first_seen_date, last_seen_date FROM shareholders "
                "WHERE id_scheme=? AND shareholder_id=? LIMIT 1",
                (scheme, sh_id)).fetchone()
            inv = sc.execute(
                "SELECT linked_records_count, verified_links_count, "
                "  inferred_links_count FROM site_investors "
                "WHERE id_scheme=? AND shareholder_id=?",
                (scheme, sh_id)).fetchone()
            ids = self._linked_identity_ids(live, sc, scheme, sh_id)
            positions = self._holder_positions(live, sc, ids, as_of_dashed)
            # CISIN collapse: two instrument records of one security
            # (Q2: 144 multi-ins CISIN groups) are ONE economic
            # position. Without this, a twin whose records sit under
            # sibling ins_codes shows twice and the value history
            # double-counts the security. Keep one position per CISIN:
            # the requested id's own record first, then the latest
            # observation. Records without a CISIN never collapse.
            if positions:
                ins_list = {h["ins_code"] for _s, h, _o, _i in positions}
                ph = ",".join("?" for _ in ins_list)
                cisin_of = {}
                for rr in live.execute(
                    "SELECT ins_code, cisin FROM companies "
                    "WHERE ins_code IN (%s)" % ph,
                    list(ins_list)).fetchall():
                    if rr["cisin"]:
                        cisin_of[rr["ins_code"]] = rr["cisin"]
                if cisin_of:
                    by_cisin = {}
                    for it in positions:
                        c = cisin_of.get(it[1]["ins_code"])
                        if c:
                            by_cisin.setdefault(c, []).append(it)
                    keep = set()
                    for items in by_cisin.values():
                        best = max(items, key=lambda it: (
                            1 if (it[2], it[3]) == (scheme, sh_id) else 0,
                            it[1]["date"] or ""))
                        keep.add(id(best))
                    positions = [it for it in positions
                                 if cisin_of.get(it[1]["ins_code"]) is None
                                 or id(it) in keep]
            # Tier by ORIGIN, not by the row's pipeline status: a position
            # under the requested id itself is that id's own direct record
            # (no link is involved, so no link uncertainty); a position
            # under a twin id rests on the inferred/verified link to it.
            # The old rule mapped 'source_id' (a share_id's own positions)
            # to 'uncertain', so a share_id portfolio looked empty even
            # when it held positions.
            twin_tier = ("confirmed" if reg and reg["identity_status"]
                         == "verified_same_source_row" else "probable")
            confirmed = []   # direct record of the requested id (or same-row link)
            probable = []    # position under an inferred twin id
            uncertain = []   # no usable link (kept for compatibility; empty now)
            total_value = 0
            probable_value = 0
            missing_price = 0
            tier_counts = {"confirmed": 0, "probable": 0, "uncertain": 0}
            valued = []  # (ins_code, origin_scheme, origin_id) for the history
            for symbol, h, origin_scheme, origin_id in positions:
                # Prefer the latest valid exchange close on/before the
                # observation date. The source holdings price/market_value
                # may be absent or stale; never substitute a future close.
                price = h.get("close_price")
                if price is None and h.get("price") is not None and h["price"] > 0:
                    price = h["price"]
                value = (h["shares"] * price
                         if h["shares"] is not None and price is not None
                         else None)
                same_date_total = h.get("same_date_total_company_shares")
                if same_date_total is None or same_date_total <= 0:
                    same_date_total = h.get("total_company_shares")
                same_date_pct = None
                if (h["shares"] is not None and same_date_total is not None
                        and same_date_total > 0):
                    same_date_pct = round(
                        h["shares"] * 100.0 / same_date_total, 6)
                pos = {
                    "symbol": symbol,
                    "observation_date_jalali": self._fmt_jalali_date(h["jalali_date"]),
                    "shares": h["shares"],
                    "ownership_percent_calculated": same_date_pct,
                    "price": price,
                    "price_date": h.get("close_price_date") or h.get("date"),
                    "price_date_jalali": (
                        self._gregorian_yyyymmdd_to_jalali(
                            h.get("close_price_date") or h.get("date"))
                        if (h.get("close_price_date") or h.get("date")) else None),
                    "valuation_status": "valued" if value is not None else "missing_price",
                    "holding_value_rial": value,
                    "id_scheme": origin_scheme,
                    "shareholder_id": origin_id,
                    "identity_status": h["identity_status"],
                }
                tier = ("confirmed" if (origin_scheme, origin_id) == (scheme, sh_id)
                        else twin_tier)
                tier_counts[tier] += 1
                if tier in ("confirmed", "probable"):
                    valued.append((h["ins_code"], origin_scheme, origin_id))
                if tier == "confirmed":
                    confirmed.append(pos)
                    if value is None:
                        missing_price += 1
                    else:
                        total_value += value
                elif tier == "probable":
                    probable.append(pos)
                    if value is None:
                        missing_price += 1
                    else:
                        probable_value += value
                else:
                    uncertain.append(pos)
            # Rebuild the full dated history from all defensibly linked identity IDs.
            # A later company snapshot without this holder is labelled "assumed sold"
            # per the product's chosen convention, with a visible threshold caveat.
            sector_registry, sector_categories, sector_registry_metadata = \
                self._load_sector_reference(self.site_dir)
            sector_accumulator = self._sector_affinity_accumulator_factory(
                registry=sector_registry,
                categories=sector_categories,
                requested_pair=(scheme, sh_id),
                as_of_date=as_of_dashed,
                half_life_months=6.0,
                max_price_age_days=62,
                rotation_lookback_days=90,
                rotation_scale_pct=5.0,
            )
            historical_summary, historical_symbols, history_series = \
                self._build_portfolio_history_summary(
                    self._iter_portfolio_history_observations(
                        live, ids, (scheme, sh_id), as_of_dashed=as_of_dashed),
                    sector_accumulator=sector_accumulator)
            sector_affinity = sector_accumulator.result()
            sector_affinity["registry"] = sector_registry_metadata
            if historical_symbols:
                ins_codes = list(dict.fromkeys(
                    item["ins_code"] for item in historical_symbols if item.get("ins_code")))
                latest_company_dates = {}
                # Stay below SQLite's bound-parameter limit even for investors
                # with unusually broad historical portfolios.
                for start in range(0, len(ins_codes), 500):
                    part = ins_codes[start:start + 500]
                    placeholders = ",".join("?" for _ in part)
                    date_sql = (
                        "SELECT ins_code, MAX(date) AS latest_date FROM company_daily "
                        "WHERE ins_code IN (%s)" % placeholders)
                    date_args = list(part)
                    if as_of_dashed:
                        date_sql += " AND date<=?"
                        date_args.append(as_of_dashed)
                    date_sql += " GROUP BY ins_code"
                    latest_company_dates.update({
                        row["ins_code"]: self._norm_date(row["latest_date"])
                        for row in live.execute(date_sql, date_args).fetchall()
                    })
                for item in historical_symbols:
                    latest_date = latest_company_dates.get(item["ins_code"])
                    item["latest_company_observation_date"] = latest_date
                    item["exit_status"] = self._classify_historical_exit(
                        item.get("last_observation_date"), latest_date,
                        item.get("last_observation_shares"))
                    item["exit_status_label"] = {
                        "assumed_sold": "assumed_sold",
                        "currently_disclosed": "currently_disclosed",
                        "unknown": "unknown",
                    }[item["exit_status"]]
            reg_status = reg["identity_status"] if reg else None
            evidence = {
                # The pipeline's own verdict for this id, plus the raw
                # facts behind it, so the user can see WHY the identity
                # is (or is not) trusted -- not just a badge.
                "registry_identity_status": reg_status,
                "shareholder_type": reg["shareholder_type"] if reg else None,
                "first_seen_date_jalali":
                    self._gregorian_yyyymmdd_to_jalali(reg["first_seen_date"])
                    if reg and reg["first_seen_date"] else None,
                "last_seen_date_jalali":
                    self._gregorian_yyyymmdd_to_jalali(reg["last_seen_date"])
                    if reg and reg["last_seen_date"] else None,
                "linked_records_count":
                    inv["linked_records_count"] if inv else len(positions),
                "verified_links_count":
                    inv["verified_links_count"] if inv else tier_counts["confirmed"],
                "inferred_links_count":
                    inv["inferred_links_count"] if inv else tier_counts["probable"],
                "positions_by_tier": tier_counts,
                # Which (scheme, id) pairs were merged into this portfolio,
                # from the pipeline's own raw_source_ids (see
                # _linked_identity_ids). The user sees exactly what was
                # treated as one person -- and what was not.
                "linked_identity_ids":
                    ["%s:%d" % (s, i) for s, i in ids],
            }
        finally:
            live.close()
            sc.close()
        return {
            "investor": {
                "id": investor_id,
                "canonical_name": reg["name"] if reg else "نامشخص",
                "verification_status":
                    reg_status if reg_status else "unresolved",
            },
            "identity_evidence": evidence,
            "summary": {
                "confirmed_combined_value_rial": total_value,
                "probable_combined_value_rial": probable_value,
                "positions_missing_price_count": missing_price,
                "confirmed_companies_count": len(confirmed),
                "probable_companies_count": len(probable),
                "uncertain_candidates_count": len(uncertain),
            },
            "history_series": history_series,
            "sector_affinity": sector_affinity,
            "historical_summary": historical_summary,
            "historical_symbols": historical_symbols,
            "confirmed_positions": confirmed,
            "probable_positions": probable,
            "uncertain_candidates": uncertain,
        }

