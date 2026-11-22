# server.py -- API backend for the tsetmc_site dashboard (ai_studio_code.html)
#
# The dashboard is a single-file React app that expects a REST API on
# http://127.0.0.1:8000 with endpoints under /api/v1/*. This server is that
# backend. It is stdlib-only (no pip installs): http.server + sqlite3.
#
# DATA SOURCES
#   * The live tsetmc_holdings SQLite database -- opened READ-ONLY
#     (mode=ro + PRAGMA query_only). The server NEVER writes to it.
#     The Excel workbooks in E:\tsetmc_holdings\output are exports of this
#     same database, so serving from the DB is faster and always complete;
#     the output folder is only listed for the admin manifests view.
#   * A small sidecar database (site_data.sqlite, next to this script) holds
#     the server's own state: watchlist, saved screener filters, and
#     materialised summary tables rebuilt from the live DB.
#
# RUN (on the laptop, from the tsetmc_site folder):
#   C:\Python313\python.exe server.py
#   then open http://127.0.0.1:8000/ in the browser.
# Options: --db <path> --port 8000 --host 127.0.0.1 --refresh-minutes 10
# The first start builds summary tables from the live DB in the background
# (the holdings table is large, so the first build can take a while); the
# dashboard answers once it finishes. Later starts reuse the built summaries
# instantly when the database file has not changed.
#
# ENDPOINTS (the exact contract the dashboard calls):
#   GET  /api/v1/market/overview
#   GET  /api/v1/market/watch?q&market&sort_by&sort_dir&limit&offset
#   GET  /api/v1/stocks/search?q&limit
#   GET  /api/v1/stocks/<symbol>
#   GET  /api/v1/stocks/<symbol>/shareholders?sort_by&sort_dir&limit&date_jalali
#   GET  /api/v1/stocks/<symbol>/ownership-series?id_scheme&shareholder_id
#   GET  /api/v1/stocks/<symbol>/corporate-actions
#   GET  /api/v1/stocks/<symbol>/data-quality
#   GET  /api/v1/stocks/<symbol>/prices   (daily OHLCV from trading_days)
#   GET  /api/v1/shareholders/search?q&symbol&shareholder_type&multi_company_only&limit&offset&min_ownership
#   GET  /api/v1/shareholders/observation-history?symbol&id_scheme&shareholder_id
#   GET  /api/v1/portfolios/investors?q&limit
#   GET  /api/v1/portfolios/<id>?as_of_date
#   POST /api/v1/screener/query        POST /api/v1/screener/export (CSV)
#   GET  /api/v1/screener/saved-filters   POST /api/v1/screener/saved-filters
#   GET  /api/v1/watchlists/default
#   POST /api/v1/watchlists/default/symbols
#   DELETE /api/v1/watchlists/default/symbols/<symbol>
#   GET  /api/v1/admin/overview   GET /api/v1/admin/manifests
#   GET  /api/v1/admin/errors     GET /api/v1/admin/identity/queue
#   POST /api/v1/admin/ingestion/trigger  (not wired -- see handler)
#   POST /api/v1/admin/identity/resolve   (not wired -- see handler)

import argparse
import csv
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Jalali (Persian) calendar conversion -- pure Python, no dependency.
# Arithmetic Jalali calendar (33-year cycles, leap years 1, 5, 9, 13, 17,
# 22, 26, 30 of each cycle), anchored on the epoch 622-03-21 Gregorian
# (Jalali 1-01-01). Verified against known dates (2026-03-21 == 1405-01-01,
# 2026-11-18 == 1405-08-27, 2008-12-06 == 1387-09-16) and against the live
# DB's own date <-> jalali_date pairs at startup (see _verify_jalali).

_JALALI_EPOCH_JDN = 1948320  # JDN of 622-03-21 Gregorian == Jalali 1-01-01
_CYCLE_DAYS = 12053          # 33*365 + 8 leap days
_CYCLE_LEAP_YEARS = (0, 4, 8, 12, 16, 21, 25, 29)  # 0-based years in a cycle


def _gregorian_to_jdn(gy, gm, gd):
    a = (14 - gm) // 12
    y = gy + 4800 - a
    m = gm + 12 * a - 3
    return (gd + (153 * m + 2) // 5 + 365 * y + y // 4
            - y // 100 + y // 400 - 32045)


def _jdn_to_gregorian(jdn):
    a = jdn + 32044
    b = (4 * a + 3) // 146097
    c = a - (146097 * b) // 4
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    day = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year = 100 * b + d - 4800 + (m // 10)
    return year, month, day


def _jalali_is_leap(jy):
    return ((jy - 1) % 33) in _CYCLE_LEAP_YEARS


def _jdn_to_jalali(jdn):
    days = jdn - _JALALI_EPOCH_JDN
    cycles = days // _CYCLE_DAYS
    rem = days % _CYCLE_DAYS
    year = cycles * 33 + 1
    for y in range(33):
        diy = 366 if y in _CYCLE_LEAP_YEARS else 365
        if rem < diy:
            year += y
            doy = rem
            break
        rem -= diy
    else:  # pragma: no cover - cannot happen for a valid cycle
        year += 32
        doy = rem
    if doy < 186:
        month = doy // 31 + 1
        day = doy % 31 + 1
    else:
        doy -= 186
        month = doy // 30 + 7
        day = doy % 30 + 1
    return year, month, day


def _jalali_to_jdn(jy, jm, jd):
    cycles = (jy - 1) // 33
    y = (jy - 1) % 33
    days = cycles * _CYCLE_DAYS
    for i in range(y):
        days += 366 if i in _CYCLE_LEAP_YEARS else 365
    if jm <= 6:
        days += (jm - 1) * 31 + (jd - 1)
    else:
        days += 186 + (jm - 7) * 30 + (jd - 1)
    return _JALALI_EPOCH_JDN + days


def gregorian_to_jalali(gy, gm, gd):
    return _jdn_to_jalali(_gregorian_to_jdn(gy, gm, gd))


def jalali_to_gregorian(jy, jm, jd):
    return _jdn_to_gregorian(_jalali_to_jdn(jy, jm, jd))


def _norm_date(s, sep="-"):
    """Canonicalise a date to 'YYYY-MM-DD'.

    Accepts '20261118', '2026-11-18' and '2026/11/18' (the live DB stores
    Gregorian dates dashed and Jalali dates slashed, so both appear).
    """
    s = str(s or "").strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8])
    return s


def fmt_gregorian_date(yyyymmdd):
    """Any Gregorian date form -> 'YYYY-MM-DD'."""
    return _norm_date(yyyymmdd)


def fmt_jalali_date(yyyymmdd):
    """Any Jalali date form -> 'YYYY-MM-DD'."""
    return _norm_date(yyyymmdd)


def gregorian_yyyymmdd_to_jalali(yyyymmdd):
    s = str(yyyymmdd or "").replace("-", "").replace("/", "")
    if len(s) == 8 and s.isdigit():
        jy, jm, jd = gregorian_to_jalali(int(s[0:4]), int(s[4:6]), int(s[6:8]))
        return "%04d-%02d-%02d" % (jy, jm, jd)
    return fmt_jalali_date(yyyymmdd)


def jalali_to_gregorian_date(jalali):
    """'1405-08-27', '14050827' or '1405/08/27' -> '2026-11-18'
    (dashed Gregorian, the form the live DB stores dates in)."""
    s = str(jalali or "").replace("-", "").replace("/", "")
    if len(s) == 8 and s.isdigit():
        try:
            gy, gm, gd = jalali_to_gregorian(int(s[0:4]), int(s[4:6]), int(s[6:8]))
            return "%04d-%02d-%02d" % (gy, gm, gd)
        except Exception:
            return ""
    return ""


def _verify_jalali(con):
    """Check the converter against the DB's own date<->jalali_date pairs."""
    try:
        rows = con.execute(
            "SELECT date, jalali_date FROM shareholder_holdings "
            "WHERE jalali_date IS NOT NULL LIMIT 5").fetchall()
        bad = 0
        for g, j in rows:
            if gregorian_yyyymmdd_to_jalali(g) != fmt_jalali_date(j):
                bad += 1
        if rows and bad == 0:
            print("[jalali] converter verified against %d DB rows" % len(rows))
        elif rows:
            print("[jalali] WARNING: %d/%d DB rows disagree with converter"
                  % (bad, len(rows)))
    except Exception as e:
        print("[jalali] verification skipped: %r" % (e,))

# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def open_live_db(db_path):
    """Open the pipeline DB strictly read-only. Never writes to it."""
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON")
    # The per-symbol endpoints do many primary-key lookups; the default
    # 2 MB cache would re-read the same index pages from disk every time.
    con.execute("PRAGMA cache_size = -64000")
    con.row_factory = sqlite3.Row
    return con

def open_sidecar_db(path):
    con = sqlite3.connect(path, uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    # Readers wait (instead of failing instantly) while the refresh
    # thread rebuilds a sidecar table or builds an index: index builds
    # on millions of rows take over a minute.
    con.execute("PRAGMA busy_timeout = 120000")
    # The sidecar holds millions of rows; the search/screener/portfolio
    # endpoints join its tables per row, so give them room to cache.
    con.execute("PRAGMA cache_size = -262144")
    return con

# ---------------------------------------------------------------------------
# Identity tiers (the pipeline's real identity_status vocabulary)
# ---------------------------------------------------------------------------
# The pipeline (services/xref.py) emits exactly these statuses for a
# holder's identity link -- the frontend used to look for "verified",
# which the pipeline NEVER emits, so every investor showed 0 confirmed
# links and every badge fell back to "unresolved". The tiers below are
# the pipeline's own:
#   verified_same_source_row -- the source itself placed both ids in one
#       row (the strongest link: a record id and a stable share_id were
#       seen together in one TSETMC response row).
#   inferred_exact_match -- weaker by design: the link was inferred only
#       when ALL hold: the two ids coincide exactly (same date, same share
#       count, same normalised name), the candidate is unique, no date
#       contradicts it, and masked natural persons ("شخص حقیقی") need >= 2
#       independent matching dates. Names are only a consistency check on
#       top of exact numeric equality, never alone.
#   unlinked_record_id / ambiguous_not_linked / source_id -- no usable
#       link: the holder is one record only, ambiguous, or has no xref
#       information at all. These stay uncertain candidates.
IDENTITY_CONFIRMED = "verified_same_source_row"
IDENTITY_PROBABLE = "inferred_exact_match"
IDENTITY_UNCERTAIN = ("unlinked_record_id", "ambiguous_not_linked",
                      "source_id")

def identity_tier(status):
    """'confirmed' | 'probable' | 'uncertain' for a pipeline identity_status."""
    if status == IDENTITY_CONFIRMED:
        return "confirmed"
    if status == IDENTITY_PROBABLE:
        return "probable"
    return "uncertain"

def _latest_close(live, ins_code, on_or_before):
    """Latest closing price for a symbol on/before a YYYY-MM-DD date.

    Prices come from trading_days (p_closing), NOT from the price column
    of shareholder_holdings: on the real database that column is NULL on
    the latest holdings date for 573 of 608 symbols, so any valuation
    built on it is NULL almost everywhere (market_value, portfolio
    values, event values). trading_days is the authoritative price
    source and the /prices endpoint reads it too.
    """
    r = live.execute(
        "SELECT p_closing FROM trading_days WHERE ins_code=? AND date<=? "
        "AND p_closing IS NOT NULL AND p_closing > 0 "
        "ORDER BY date DESC LIMIT 1",
        (ins_code, on_or_before)).fetchone()
    return r["p_closing"] if r else None

# Sidecar layout version. Bumped whenever the refresh builds a new table
# or column: refresh() forces a full rebuild when the stored version
# differs, so a code upgrade never serves a stale sidecar through the
# fingerprint shortcut (the DB file itself has not changed).
SIDECAR_VERSION = 7

# Persian/Arabic letter variants: the pipeline stores symbols and names in
# the form TSETMC returned them, so the same word can appear with Arabic
# ي/ك or Persian ی/ک (e.g. فملي vs فملی). User input is almost always
# Persian, so a plain LIKE misses the Arabic-form rows. Normalising both
# sides to Persian (and, for LIKE patterns, also trying the swapped form)
# makes symbol/name search find both spellings.
_FA_NORM = {"\u0643": "\u06a9", "\u064a": "\u06cc",
            "\u0649": "\u06cc", "\u06c0": "\u0647", "\u0629": "\u0647"}


def normalize_fa(s):
    """Map Arabic ي/ك (and ى/ۀ/ة) to their Persian forms; strip invisibles."""
    if s is None:
        return ""
    out = []
    for ch in str(s):
        if ch in _FA_NORM:
            out.append(_FA_NORM[ch])
        elif ch in ("\u200c", "\u200d", "\ufeff"):
            continue
        else:
            out.append(ch)
    return "".join(out).strip()


def _like_variants(q):
    """LIKE patterns for a query in both spellings: as typed, and with
    Persian ی/ک swapped to Arabic ي/ك (covers DB rows stored either way)."""
    base = str(q)
    swapped = base.replace("\u06cc", "\u064a").replace("\u06a9", "\u0643")
    pats = [_like(base)]
    if swapped != base:
        pats.append(_like(swapped))
    return pats


def _like_or_clause(columns, q):
    """SQL fragment: (col LIKE ? OR col LIKE ? OR ...) over the spelling
    variants of q for each column; returns (fragment, args)."""
    parts, args = [], []
    for col in columns:
        for pat in _like_variants(q):
            parts.append("%s LIKE ?" % col)
            args.append(pat)
    return "(" + " OR ".join(parts) + ")", args

# ---------------------------------------------------------------------------
# Sidecar state + materialised summaries
# ---------------------------------------------------------------------------

SIDECAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    filter_ast_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_watch (
    ins_code TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    company_name TEXT,
    market TEXT NOT NULL,
    first_date TEXT,
    first_date_jalali TEXT,
    latest_date TEXT,
    latest_date_jalali TEXT,
    total_company_shares INTEGER,
    disclosed_shareholder_count INTEGER,
    total_shareholder_count INTEGER,
    total_shareholder_count_note TEXT,
    disclosed_ownership_percent_sum REAL,
    data_completeness TEXT,
    -- Identity-resolved latest snapshot. Only verified/inferred xref
    -- links and the provable-majority twin rule collapse holders. Source-
    -- proven mirrored/repeated ondate rows are excluded separately.
    resolved_shareholder_count INTEGER,
    resolved_ownership_percent_sum REAL,
    resolved_unresolved_count INTEGER,
    resolved_status TEXT,
    resolved_status_note TEXT,
    raw_ownership_percent_sum REAL,
    -- Latest price (from trading_days: the latest closing price) and
    -- market value (total shares x latest price). Price is the newest
    -- available closing price, not necessarily on the latest holdings
    -- date. (The price column of shareholder_holdings was tried first;
    -- on the real DB it is NULL on the latest date for 573/608 symbols.)
    latest_price REAL,
    latest_price_date TEXT,
    market_value REAL
);
CREATE TABLE IF NOT EXISTS site_company_daily_dedup (
    ins_code TEXT NOT NULL,
    date TEXT NOT NULL,
    resolved_count INTEGER,
    resolved_shares INTEGER,
    resolved_pct REAL,
    raw_count INTEGER,
    raw_pct REAL,
    raw_shares INTEGER,
    unquantified_count INTEGER,
    unresolved_count INTEGER,
    unresolved_shares INTEGER,
    total_shares INTEGER,
    status TEXT,
    status_note TEXT,
    PRIMARY KEY (ins_code, date)
);
CREATE TABLE IF NOT EXISTS site_excluded_holder_rows (
    ins_code TEXT NOT NULL,
    date TEXT NOT NULL,
    id_scheme TEXT NOT NULL,
    shareholder_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (ins_code, date, id_scheme, shareholder_id)
);
CREATE TABLE IF NOT EXISTS site_holder_latest (
    ins_code TEXT NOT NULL,
    symbol TEXT NOT NULL,
    id_scheme TEXT NOT NULL,
    shareholder_id INTEGER NOT NULL,
    identity_status TEXT,
    latest_date TEXT NOT NULL,
    latest_date_jalali TEXT,
    shares INTEGER,
    ownership_percent_calc REAL,
    ownership_percent_reported REAL,
    shares_change INTEGER,
    change_type TEXT,
    entry_status TEXT,
    price REAL,
    market_value REAL,
    PRIMARY KEY (ins_code, id_scheme, shareholder_id)
);
-- Holder names/types, copied from the live registry by one sequential scan.
-- Kept separate from site_holder_latest on purpose: joining the registry
-- per holdings row during the refresh never finishes on the real database
-- (13.8M random lookups); endpoints join this small table instead.
CREATE TABLE IF NOT EXISTS site_shareholder_names (
    id_scheme TEXT NOT NULL,
    shareholder_id INTEGER NOT NULL,
    name TEXT,
    shareholder_type TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    PRIMARY KEY (id_scheme, shareholder_id)
);
-- One row per holder: how many symbols they appear in, how many of
-- those links are verified (same-source-row) and how many are exact-match
-- inferred. Built in the refresh so the investors list never scans the
-- whole holder table per request. (The old code counted
-- identity_status='verified', a status the pipeline never emits, so every
-- investor showed 0 verified links.)
CREATE TABLE IF NOT EXISTS site_investors (
    id_scheme TEXT NOT NULL,
    shareholder_id INTEGER NOT NULL,
    linked_records_count INTEGER NOT NULL,
    verified_links_count INTEGER NOT NULL,
    inferred_links_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id_scheme, shareholder_id)
);
CREATE TABLE IF NOT EXISTS site_symbol_holdings (
    ins_code TEXT PRIMARY KEY,
    holdings_rows INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_corp_action_counts (
    ins_code TEXT PRIMARY KEY,
    n INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

def _market_for_display(market):
    """Universe markets -> the dashboard's TSE/IFB/UNKNOWN filter values."""
    if market in ("TSE", "IFB"):
        return market
    if market == "IFB_BASE":
        return "IFB"
    return "UNKNOWN"

def _stream_insert(cur, sidecar, select_sql, insert_sql, transform,
                   label, batch=20000):
    """Run a big SELECT and stream its rows into a sidecar table in batches.

    The refresh used to fetchall() multi-million-row results into one Python
    list first: on the real database that alone grows past 2 GB and takes
    minutes before a single row is written. Streaming keeps memory flat
    (~one batch) and logs progress, so a stuck refresh is visible.
    The caller must hold the sidecar transaction (``with sidecar:``) around
    the call, after deleting the target table's old rows.
    """
    n = 0
    t0 = time.time()
    # execute() returns a cursor whether cur is a cursor or a connection;
    # fetchmany() lives on the cursor, not on the connection.
    cursor = cur.execute(select_sql)
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        sidecar.executemany(insert_sql, [transform(r) for r in rows])
        n += len(rows)
        if n % 1000000 < batch:
            print("[refresh] %s: %d rows (%.1fs)"
                  % (label, n, time.time() - t0), flush=True)
    return n, time.time() - t0


def _duplicate_ondate_source_ids(live):
    """Return source IDs that are proven extra copies in a repeated endpoint
    response. This is deliberately stricter than matching equal share counts:
    at least 3 different business-row signatures must occur exactly twice,
    those doubled signatures must represent at least 80% of all signatures,
    and no signature may occur more than twice. Rows are grouped by the exact
    instrument/date/endpoint/request reference. For each pair, retain the
    smaller source ID and exclude only the larger one from summary totals.
    """
    exists = live.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='source_observations'").fetchone()
    if not exists:
        return set()

    duplicate_ids = set()
    qualifying_groups = 0
    current_key = None
    signature_counts = Counter()
    signature_ids = defaultdict(list)

    def _number(value):
        if value is None:
            return None
        try:
            n = float(value)
            if abs(n - round(n)) < 1e-9:
                return int(round(n))
            return round(n, 6)
        except (TypeError, ValueError, OverflowError):
            return str(value)

    def _flush():
        nonlocal qualifying_groups
        if current_key is None or len(signature_counts) < 3:
            return
        doubled = [sig for sig, count in signature_counts.items()
                   if count == 2]
        if (len(doubled) < 3
                or len(doubled) / len(signature_counts) < 0.80
                or any(count > 2 for count in signature_counts.values())):
            return

        # Validate every pair before excluding any row from this response.
        extra_ids = []
        for sig in doubled:
            ids = sorted(set(signature_ids[sig]))
            if len(ids) != 2 or ids[0] is None or ids[1] is None:
                return
            extra_ids.append(
                (current_key[0], current_key[1], int(ids[1])))
        duplicate_ids.update(extra_ids)
        qualifying_groups += 1

    cursor = live.execute(
        "SELECT ins_code, row_date, endpoint, request_ref, name, shares, "
        "percent, change_code, change_amount, source_id "
        "FROM source_observations "
        "WHERE endpoint='shareholders_on_date' "
        "AND row_date IS NOT NULL AND request_ref IS NOT NULL "
        "AND shares IS NOT NULL AND percent IS NOT NULL "
        "ORDER BY ins_code, row_date, endpoint, request_ref, source_id")
    while True:
        batch = cursor.fetchmany(20000)
        if not batch:
            break
        for row in batch:
            key = (row[0], row[1], row[2], row[3])
            if key != current_key:
                _flush()
                current_key = key
                signature_counts = Counter()
                signature_ids = defaultdict(list)
            signature = (
                normalize_fa(row[4] or ""),
                _number(row[5]),
                _number(row[6]),
                _number(row[7]),
                _number(row[8]),
            )
            signature_counts[signature] += 1
            signature_ids[signature].append(row[9])
    _flush()

    print("[refresh] repeated endpoint groups: %d; duplicate rows excluded: %d"
          % (qualifying_groups, len(duplicate_ids)), flush=True)
    return duplicate_ids


def _mirrored_source_snapshot_dates(live):
    """Find dates where the date-specific and current-holder endpoints are
    two different-ID views of the same ownership snapshot.

    Source IDs and change metadata are intentionally excluded: the endpoints
    use different ID schemes and may report different change_amount values.
    We compare name, shares and reported ownership percent as a multiset, so
    duplicate row multiplicity must also match. Only exact whole-snapshot
    matches qualify; partial overlaps are never merged automatically.
    """
    exists = live.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='source_observations'").fetchone()
    if not exists:
        return set()

    mirrored = set()
    current_key = None
    endpoint_rows = {}

    def _flush():
        if current_key is None:
            return
        dated = endpoint_rows.get("shareholders_on_date")
        latest = endpoint_rows.get("shareholder_last")
        if dated and latest and dated == latest:
            mirrored.add(current_key)

    def _number(value):
        if value is None:
            return None
        try:
            n = float(value)
            if abs(n - round(n)) < 1e-9:
                return int(round(n))
            return round(n, 6)
        except (TypeError, ValueError, OverflowError):
            return str(value)

    try:
        cursor = live.execute(
            "SELECT ins_code, row_date, endpoint, name, shares, percent "
            "FROM source_observations "
            "WHERE endpoint IN ('shareholders_on_date', 'shareholder_last') "
            "ORDER BY ins_code, row_date, endpoint")
        while True:
            batch = cursor.fetchmany(20000)
            if not batch:
                break
            for row in batch:
                key = (row[0], row[1])
                if key != current_key:
                    _flush()
                    current_key = key
                    endpoint_rows = {}
                signature = (
                    normalize_fa(row[3] or ""),
                    _number(row[4]),
                    _number(row[5]),
                )
                endpoint_rows.setdefault(row[2], Counter())[signature] += 1
        _flush()
    except sqlite3.Error as exc:
        print("[refresh] source-view comparison skipped: %r" % (exc,),
              flush=True)
        return set()

    print("[refresh] exact mirrored endpoint snapshots: %d"
          % len(mirrored), flush=True)
    return mirrored


def _collect_excluded_holder_rows(live, sidecar):
    """Materialize source-proven duplicate ondate rows for all dashboard views.

    The aggregate series, latest-holder index, detail table and per-holder
    histories must share one exclusion set. Only ondate_share_id rows backed
    by exact repeated-response evidence or a complete endpoint mirror qualify.
    """
    mirrored = _mirrored_source_snapshot_dates(live)
    repeated = _duplicate_ondate_source_ids(live)
    excluded = set()
    cursor = live.execute(
        "SELECT ins_code, date, shareholder_id "
        "FROM shareholder_holdings "
        "WHERE id_scheme='ondate_share_id' "
        "ORDER BY ins_code, date")
    while True:
        rows = cursor.fetchmany(20000)
        if not rows:
            break
        for ins_code, date, shareholder_id in rows:
            if shareholder_id is None:
                continue
            sid = int(shareholder_id)
            key = (ins_code, date, sid)
            if (ins_code, date) in mirrored:
                excluded.add(key)
            elif key in repeated:
                excluded.add(key)

    with sidecar:
        sidecar.execute("DELETE FROM site_excluded_holder_rows")
        if excluded:
            sidecar.executemany(
                "INSERT OR REPLACE INTO site_excluded_holder_rows "
                "(ins_code, date, id_scheme, shareholder_id, reason) "
                "VALUES (?,?,?,?,?)",
                [(i, d, "ondate_share_id", sid,
                  "mirrored_endpoint_snapshot" if (i, d) in mirrored
                  else "repeated_endpoint_row")
                 for i, d, sid in excluded])
    print("[refresh] excluded source rows: %d" % len(excluded), flush=True)
    return mirrored, repeated, excluded


def refresh_summaries(live, sidecar):
    """Rebuild the materialised summary tables from the live DB.

    Runs at startup and every --refresh-minutes in the background.
    These are the only heavy queries; every endpoint then reads the small
    sidecar tables and answers in milliseconds.
    """
    t0 = time.time()
    print("[refresh] rebuilding summaries from the live DB ...", flush=True)
    # Invalidate readiness BEFORE touching any derived table. Any failure in
    # exclusion collection or an earlier refresh stage must not leave a stale
    # completion marker that lets API handlers serve a partial/old sidecar.
    with sidecar:
        sidecar.execute(
            "DELETE FROM site_meta WHERE key IN "
            "('last_refresh', 'ownership_resolver_schema')")
    cur = live.cursor()
    mirrored_snapshot_dates, repeated_source_ids, excluded_holder_rows = \
        _collect_excluded_holder_rows(live, sidecar)
    # Ensure this function also works when called with a raw connection
    # outside SiteApi.live() (for example, in a focused test).
    attached = {r[1] for r in live.execute("PRAGMA database_list")}
    if "sidecar_data" not in attached:
        sidecar_path = sidecar.execute("PRAGMA database_list").fetchone()[2]
        live.execute("ATTACH DATABASE ? AS sidecar_data", (sidecar_path,))
    # A generous page cache: the refresh does large index scans and many
    # primary-key lookups; the default 2 MB cache would thrash on a big DB.
    try:
        cur.execute("PRAGMA cache_size = -64000")
    except Exception:
        pass
    # 1. Per-symbol latest daily snapshot (company_daily is the build's
    #    per-symbol daily aggregate: counts, pct sums, totals, completeness).
    rows = cur.execute(
        "SELECT cd.ins_code AS ins_code, cd.symbol AS symbol, "
        "  c.name AS company_name, "
        "  COALESCE(u.market, 'UNKNOWN') AS market_raw, "
        "  m.first_date AS first_date, "
        "  cd.date AS latest_date, cd.total_company_shares AS total_company_shares, "
        "  cd.disclosed_shareholder_count AS disclosed_shareholder_count, "
        "  cd.total_shareholder_count AS total_shareholder_count, "
        "  cd.total_shareholder_count_note AS total_shareholder_count_note, "
        "  cd.disclosed_ownership_percent_sum AS disclosed_ownership_percent_sum, "
        "  cd.data_completeness AS data_completeness "
        "FROM company_daily cd "
        "JOIN (SELECT ins_code, MIN(date) AS first_date, MAX(date) AS d "
        "      FROM company_daily GROUP BY ins_code) m "
        "  ON m.ins_code = cd.ins_code AND cd.date = m.d "
        "LEFT JOIN companies c ON c.ins_code = cd.ins_code "
        "LEFT JOIN equity_universe u ON u.ins_code = cd.ins_code").fetchall()
    with sidecar:
        # DROP+CREATE (not just DELETE): the table gained deduped/latest-
        # price columns in sidecar v2, and CREATE TABLE IF NOT EXISTS
        # would leave an old sidecar without them.
        sidecar.execute("DROP TABLE IF EXISTS site_watch")
        sidecar.execute(
            "CREATE TABLE site_watch ("
            " ins_code TEXT PRIMARY KEY, symbol TEXT NOT NULL,"
            " company_name TEXT, market TEXT NOT NULL,"
            " first_date TEXT, first_date_jalali TEXT,"
            " latest_date TEXT, latest_date_jalali TEXT,"
            " total_company_shares INTEGER,"
            " disclosed_shareholder_count INTEGER,"
            " total_shareholder_count INTEGER,"
            " total_shareholder_count_note TEXT,"
            " disclosed_ownership_percent_sum REAL,"
            " data_completeness TEXT,"
            " resolved_shareholder_count INTEGER,"
            " resolved_ownership_percent_sum REAL,"
            " resolved_unresolved_count INTEGER,"
            " resolved_status TEXT,"
            " resolved_status_note TEXT,"
            " raw_ownership_percent_sum REAL,"
            " latest_price REAL, latest_price_date TEXT,"
            " market_value REAL)")
        sidecar.executemany(
            "INSERT INTO site_watch (ins_code, symbol, company_name, market, "
            " first_date, first_date_jalali, latest_date, latest_date_jalali, "
            " total_company_shares, "
            " disclosed_shareholder_count, total_shareholder_count, "
            " total_shareholder_count_note, "
            " disclosed_ownership_percent_sum, data_completeness, "
            " resolved_shareholder_count, resolved_ownership_percent_sum, "
            " resolved_unresolved_count, resolved_status, resolved_status_note, "
            " raw_ownership_percent_sum, latest_price, latest_price_date, "
            " market_value) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["ins_code"], r["symbol"], r["company_name"],
              _market_for_display(r["market_raw"]),
              _norm_date(r["first_date"]),
              gregorian_yyyymmdd_to_jalali(r["first_date"]),
              _norm_date(r["latest_date"]),
              gregorian_yyyymmdd_to_jalali(r["latest_date"]),
              r["total_company_shares"], r["disclosed_shareholder_count"],
              r["total_shareholder_count"], r["total_shareholder_count_note"],
              r["disclosed_ownership_percent_sum"],
              r["data_completeness"],
              None, None, None, None, None,
              r["disclosed_ownership_percent_sum"],
              None, None, None) for r in rows])
    print("[refresh] site_watch: %d symbols in %.1fs" % (len(rows), time.time() - t0),
          flush=True)
    # 2. Latest observation per (symbol, holder): the holder search, the
    #    screener, the investors list and the portfolios all read this.
    #    SQLite's bare-columns-with-MAX rule: with exactly one MAX() aggregate
    #    the other columns of the grouped row come from the row holding the
    #    maximum date -- so this is a single covering-index scan.
    #    NOTE: an earlier version also LEFT JOINed the shareholders registry
    #    here to attach names. That never finishes on the real database:
    #    the join runs once per holdings row (13.8M random primary-key
    #    lookups into the registry), the server sat at ~0 CPU for 6+ minutes
    #    waiting on disk. Names now live in site_shareholder_names (step 5,
    #    one sequential copy) and endpoints join that table instead.
    t1 = time.time()
    with sidecar:
        # DROP+CREATE (not just DELETE): older sidecars still carry the
        # shareholder_name/shareholder_type columns this table used to have.
        sidecar.execute("DROP TABLE IF EXISTS site_holder_latest")
        sidecar.execute(
            "CREATE TABLE site_holder_latest ("
            " ins_code TEXT NOT NULL, symbol TEXT NOT NULL,"
            " id_scheme TEXT NOT NULL, shareholder_id INTEGER NOT NULL,"
            " identity_status TEXT, latest_date TEXT NOT NULL,"
            " latest_date_jalali TEXT, shares INTEGER,"
            " ownership_percent_calc REAL, ownership_percent_reported REAL,"
            " shares_change INTEGER, change_type TEXT, entry_status TEXT,"
            " price REAL, market_value REAL,"
            " PRIMARY KEY (ins_code, id_scheme, shareholder_id))")
        n, dt = _stream_insert(
            cur, sidecar,
            "SELECT ins_code AS ins_code, symbol AS symbol, "
            "  id_scheme AS id_scheme, shareholder_id AS shareholder_id, "
            "  identity_status AS identity_status, MAX(date) AS latest_date, "
            "  jalali_date AS latest_date_jalali, shares AS shares, "
            "  ownership_percent_calc AS ownership_percent_calc, "
            "  ownership_percent AS ownership_percent_reported, "
            "  shares_change AS shares_change, change_type AS change_type, "
            "  entry_status AS entry_status, price AS price, "
            "  market_value AS market_value "
            "FROM shareholder_holdings h "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
            "  WHERE x.ins_code=h.ins_code AND x.date=h.date "
            "    AND x.id_scheme=h.id_scheme "
            "    AND x.shareholder_id=h.shareholder_id) "
            "GROUP BY h.ins_code, h.id_scheme, h.shareholder_id",
            "INSERT INTO site_holder_latest (ins_code, symbol, id_scheme, "
            " shareholder_id, identity_status, "
            " latest_date, latest_date_jalali, shares, ownership_percent_calc, "
            " ownership_percent_reported, shares_change, change_type, entry_status, "
            " price, market_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            lambda r: (r["ins_code"], r["symbol"], r["id_scheme"],
                       r["shareholder_id"], r["identity_status"],
                       _norm_date(r["latest_date"]),
                       fmt_jalali_date(r["latest_date_jalali"]),
                       r["shares"], r["ownership_percent_calc"],
                       r["ownership_percent_reported"], r["shares_change"],
                       r["change_type"], r["entry_status"], r["price"],
                       r["market_value"]),
            "site_holder_latest")
    print("[refresh] site_holder_latest: %d rows in %.1fs"
          % (n, time.time() - t1), flush=True)
    # Indexes the later refresh steps and the endpoints need. Built here
    # (not at the end) so step 6 can group by (id_scheme, shareholder_id)
    # in index order instead of sorting millions of rows.
    with sidecar:
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_holder_scheme "
            "ON site_holder_latest(id_scheme, shareholder_id)")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_holder_latest_date "
            "ON site_holder_latest(latest_date)")
        # Composite index for the shareholders-search ordering
        # (latest_date DESC, ownership_percent_calc DESC): millions of
        # holder rows share the latest date, so the single-column index
        # above still forced a full sort of those rows on every search
        # (120 s timeout on the real DB). With this index the ORDER BY
        # is served in index order and LIMIT stops after a few rows.
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_holder_latest_pct "
            "ON site_holder_latest(latest_date DESC, "
            "ownership_percent_calc DESC)")
    # 3. Holdings rows per symbol (admin manifests view).
    t2 = time.time()
    rows = cur.execute(
        "SELECT ins_code, COUNT(*) AS n FROM shareholder_holdings "
        "GROUP BY ins_code").fetchall()
    with sidecar:
        sidecar.execute("DELETE FROM site_symbol_holdings")
        sidecar.executemany(
            "INSERT INTO site_symbol_holdings (ins_code, holdings_rows) "
            "VALUES (?,?)", [(r[0], r[1]) for r in rows])
    print("[refresh] site_symbol_holdings: %d symbols in %.1fs"
          % (len(rows), time.time() - t2), flush=True)
    # 4. Corporate-action counts per symbol (screener field).
    rows = cur.execute(
        "SELECT ins_code, COUNT(*) AS n FROM corporate_actions "
        "GROUP BY ins_code").fetchall()
    with sidecar:
        sidecar.execute("DELETE FROM site_corp_action_counts")
        sidecar.executemany(
            "INSERT INTO site_corp_action_counts (ins_code, n) VALUES (?,?)",
            [(r[0], r[1]) for r in rows])
    print("[refresh] site_corp_action_counts: %d symbols" % len(rows), flush=True)
    # 5. Holder names/types, copied from the live registry by ONE sequential
    #    scan (no per-row join -- see the note on step 2). Endpoints join
    #    this table when they need a name; the refresh never joins per row.
    t5 = time.time()
    with sidecar:
        sidecar.execute("DELETE FROM site_shareholder_names")
        n, _dt = _stream_insert(
            cur, sidecar,
            "SELECT id_scheme, shareholder_id, name, shareholder_type, "
            "  first_seen_date, last_seen_date FROM shareholders",
            "INSERT INTO site_shareholder_names (id_scheme, shareholder_id, "
            " name, shareholder_type, first_seen_date, last_seen_date) "
            "VALUES (?,?,?,?,?,?)",
            lambda r: (r[0], r[1], r[2], r[3],
                       _norm_date(r[4]) if r[4] else None,
                       _norm_date(r[5]) if r[5] else None),
            "site_shareholder_names")
    print("[refresh] site_shareholder_names: %d rows in %.1fs"
          % (n, time.time() - t5), flush=True)
    # 6. One row per holder with their link counts. Groups by (id_scheme,
    #    shareholder_id) in idx_holder_scheme order, so no sort is needed.
    t6 = time.time()
    with sidecar:
        # DROP+CREATE: the table gained inferred_links_count in sidecar v2.
        sidecar.execute("DROP TABLE IF EXISTS site_investors")
        sidecar.execute(
            "CREATE TABLE site_investors ("
            " id_scheme TEXT NOT NULL, shareholder_id INTEGER NOT NULL,"
            " linked_records_count INTEGER NOT NULL,"
            " verified_links_count INTEGER NOT NULL,"
            " inferred_links_count INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (id_scheme, shareholder_id))")
        n, _dt = _stream_insert(
            sidecar, sidecar,
            "SELECT id_scheme, shareholder_id, COUNT(*) AS linked, "
            "  SUM(CASE WHEN identity_status='verified_same_source_row' "
            "      THEN 1 ELSE 0 END) AS verified, "
            "  SUM(CASE WHEN identity_status='inferred_exact_match' "
            "      THEN 1 ELSE 0 END) AS inferred "
            "FROM site_holder_latest "
            "GROUP BY id_scheme, shareholder_id",
            "INSERT INTO site_investors (id_scheme, shareholder_id, "
            " linked_records_count, verified_links_count, "
            " inferred_links_count) VALUES (?,?,?,?,?)",
            lambda r: (r[0], r[1], r[2], r[3] or 0, r[4] or 0),
            "site_investors")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_investors_count "
            "ON site_investors(linked_records_count DESC)")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_investors_inferred "
            "ON site_investors(inferred_links_count DESC)")
    print("[refresh] site_investors: %d holders in %.1fs"
          % (n, time.time() - t6), flush=True)
    with sidecar:
        # Global counters the overview reads (kept here so the overview
        # endpoint never scans the 13.8M-row holdings table itself).
        try:
            # One scan serves both the row count and the latest date.
            n_hold, max_hold = cur.execute(
                "SELECT COUNT(*), MAX(date) FROM shareholder_holdings"
                ).fetchone()
            dmin, dmax = cur.execute(
                "SELECT MIN(date), MAX(date) FROM company_daily").fetchone()
            last_ts = cur.execute("SELECT MAX(ts) FROM fetch_log").fetchone()[0]
            sidecar.execute(
                "INSERT OR REPLACE INTO site_meta (key, value) VALUES ('holdings_count', ?)",
                (str(n_hold),))
            sidecar.execute(
                "INSERT OR REPLACE INTO site_meta (key, value) VALUES ('earliest_date', ?)",
                (_norm_date(dmin),))
            sidecar.execute(
                "INSERT OR REPLACE INTO site_meta (key, value) VALUES ('latest_date', ?)",
                (_norm_date(dmax),))
            sidecar.execute(
                "INSERT OR REPLACE INTO site_meta (key, value) "
                "VALUES ('latest_holdings_date', ?)",
                (_norm_date(max_hold),))
            sidecar.execute(
                "INSERT OR REPLACE INTO site_meta (key, value) VALUES ('last_fetch_ts', ?)",
                (last_ts,))
        except Exception as e:
            print("[refresh] meta counters skipped: %r" % (e,), flush=True)
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_watch_market ON site_watch(market)")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_watch_symbol ON site_watch(symbol)")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_holder_symbol "
            "ON site_holder_latest(symbol)")
    # 7. Dashboard panels, computed here (background) so the overview
    #    endpoint reads only the sidecar and answers instantly. The live
    #    equivalents would each need a full scan of the 13.8M-row holdings
    #    table; here they run once per refresh, in the background.
    try:
        lh = sidecar.execute(
            "SELECT value FROM site_meta WHERE key='latest_holdings_date'"
            ).fetchone()
        latest_hold = lh[0] if lh else None
        top = []
        if latest_hold:
            # One row per (symbol, holder) exists on the latest date, so the
            # latest-observation sidecar rows for that date are exactly the
            # live table's rows for that date. Names come from the names
            # table (10 lookups), not from the holder rows.
            for r in sidecar.execute(
                "SELECT ins_code, symbol, latest_date_jalali, shares_change, "
                "  id_scheme, shareholder_id FROM site_holder_latest "
                "WHERE latest_date = ? AND shares_change IS NOT NULL "
                "  AND shares_change <> 0 "
                "ORDER BY ABS(shares_change) DESC LIMIT 50",
                (latest_hold,)).fetchall():
                nm = sidecar.execute(
                    "SELECT name FROM site_shareholder_names "
                    "WHERE id_scheme=? AND shareholder_id=?",
                    (r[4], r[5])).fetchone()
                # Monetary value of the change: shares x latest close
                # (trading_days, same source as the site's prices). NULL
                # when the symbol has no valid close.
                close_row = cur.execute(
                    "SELECT p_closing FROM trading_days WHERE ins_code=? "
                    "AND p_closing IS NOT NULL AND p_closing > 0 "
                    "ORDER BY date DESC LIMIT 1", (r[0],)).fetchone()
                close = close_row[0] if close_row else None
                top.append({
                    "symbol": r[1],
                    "date_jalali": fmt_jalali_date(r[2]),
                    "shares_change": r[3],
                    "value_rial": (r[3] * close) if close is not None else None,
                    "id_scheme": r[4],
                    "shareholder_id": r[5],
                    "shareholder_name": (nm[0] if nm and nm[0] else "نامشخص"),
                })
        sidecar.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) "
            "VALUES ('top_changes', ?)", (json.dumps(top, ensure_ascii=False),))
        # The date the top changes belong to (all rows share it): the
        # dashboard titles the panel with it instead of repeating the
        # date on every row.
        sidecar.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) "
            "VALUES ('top_changes_date_jalali', ?)",
            (gregorian_yyyymmdd_to_jalali(latest_hold)
             if latest_hold else None,))
        # Holders first seen in the last 60 days. first_seen_date in the
        # registry is a dashed Gregorian date ('2018-05-17').
        recent = []
        ld = sidecar.execute(
            "SELECT value FROM site_meta WHERE key='latest_date'").fetchone()
        if ld and ld[0]:
            cutoff_dt = (datetime.strptime(_norm_date(ld[0]), "%Y-%m-%d")
                         - timedelta(days=60)).strftime("%Y-%m-%d")
            for r in cur.execute(
                "SELECT id_scheme, shareholder_id, name, first_seen_date "
                "FROM shareholders WHERE first_seen_date >= ? "
                "ORDER BY first_seen_date DESC LIMIT 50",
                (cutoff_dt,)).fetchall():
                pos = sidecar.execute(
                    "SELECT symbol, ownership_percent_calc "
                    "FROM site_holder_latest "
                    "WHERE id_scheme=? AND shareholder_id=? "
                    "ORDER BY latest_date DESC LIMIT 1",
                    (r[0], r[1])).fetchone()
                recent.append({
                    "symbol": pos[0] if pos else None,
                    "id_scheme": r[0],
                    "shareholder_id": r[1],
                    "shareholder_name": r[2] or "نامشخص",
                    "ownership_percent_calculated": pos[1] if pos else None,
                })
        sidecar.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) "
            "VALUES ('recent_holders', ?)",
            (json.dumps(recent, ensure_ascii=False),))
        sidecar.commit()
        print("[refresh] panels: top_changes=%d recent=%d"
              % (len(top), len(recent)), flush=True)
    except Exception as e:
        print("[refresh] panels skipped: %r" % (e,), flush=True)
    ownership_resolver_ok = False
    # 8. Identity-resolved daily ownership series. The pipeline stores the
    #    same holder under several id schemes (ondate_share_id AND
    #    share_id), so company_daily's disclosed sum counts that holder
    #    twice or three times and can exceed 100% (مهر3: 265.24%). The old
    #    code "fixed" that by keeping one row per distinct share count --
    #    which is not an identity key: two different holders can hold the
    #    same number of shares, and it also summed the stored
    #    ownership_percent_calc values, which divide historical shares by
    #    the CURRENT total (Q1 proved it wrong for pre-increase dates).
    #
    #    The new aggregation resolves each (ins_code, date) in three
    #    steps, all set-based SQL over the live DB (ATTACHed read-only):
    #    (1) canonical identity from the pipeline's own xref table
    #    (shareholder_id_xref): a row merges into its canonical holder
    #    ONLY when the xref status is verified_same_source_row or
    #    inferred_exact_match; unlinked / ambiguous / source_id rows stay
    #    themselves. (2) provable-majority twins: an unlinked ondate row
    #    and an unlinked share_id row with the same (ins, date, shares,
    #    registry name), exactly one candidate on each side, whose shares
    #    are a strict majority of the SAME-DATE issued total -- two people
    #    cannot each hold >50% of one company on one date. The twin is
    #    keyed to the share_id side. (3) everything else stays a separate
    #    holder; rows that share (date, shares, name) with another
    #    resolved holder but have no link are counted as unresolved
    #    duplicate candidates and the day is marked 'review'.
    #    Ownership is computed from shares, never by summing stored
    #    percentages: resolved_pct = resolved_shares / total_shares(date)
    #    * 100, where total_shares(date) is company_daily's same-date
    #    total (fallback: the holdings rows' own total). Totals are never
    #    clamped to 100%: a resolved total above 100% is impossible by
    #    construction (each holder counted once), so if it ever appears
    #    it means the denominator is wrong and the day is 'review'.
    #    Names are used only as a consistency check, never as identity.
    #    The live DB is ATTACHed read-only; a failure here logs and the
    #    refresh continues (endpoints fall back to company_daily, as
    #    before).
    try:
        t8 = time.time()
        live_file = None
        for _seq, _name, _file in live.execute(
                "PRAGMA database_list").fetchall():
            if _name == "main":
                live_file = _file
                break
        if not live_file:
            raise RuntimeError("live DB path not found (PRAGMA database_list)")
        live_uri = ("file:" +
                    urllib.request.pathname2url(os.path.abspath(live_file)) +
                    "?mode=ro")
        with sidecar:
            sidecar.execute("DROP TABLE IF EXISTS site_company_daily_dedup")
            sidecar.execute(
                "CREATE TABLE site_company_daily_dedup ("
                " ins_code TEXT NOT NULL, date TEXT NOT NULL,"
                " resolved_count INTEGER, resolved_shares INTEGER,"
                " resolved_pct REAL, raw_count INTEGER, raw_pct REAL,"
                " raw_shares INTEGER, unquantified_count INTEGER,"
                " unresolved_count INTEGER, unresolved_shares INTEGER,"
                " total_shares INTEGER, status TEXT, status_note TEXT,"
                " PRIMARY KEY (ins_code, date))")
        sidecar.execute("ATTACH DATABASE ? AS live", (live_uri,))
        try:
            # Same-date issued-share total per (ins_code, date).
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_totals AS "
                "SELECT h.ins_code AS ins_code, h.date AS date, "
                "  CASE WHEN MAX(cd.total_company_shares) > 0 "
                "    THEN MAX(cd.total_company_shares) "
                "    WHEN MAX(h.total_company_shares) > 0 "
                "    THEN MAX(h.total_company_shares) "
                "    ELSE NULL END AS total_shares "
                "FROM live.shareholder_holdings h "
                "LEFT JOIN live.company_daily cd "
                "  ON cd.ins_code = h.ins_code AND cd.date = h.date "
                "GROUP BY h.ins_code, h.date")
            # Raw aggregates (no joins): every source row, duplicates and
            # all, plus the unquantified rows that carry no share count.
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_raw AS "
                "SELECT ins_code, date, COUNT(*) AS raw_count, "
                "  COALESCE(SUM(ownership_percent_calc), 0.0) AS raw_pct, "
                "  COALESCE(SUM(shares), 0) AS raw_shares, "
                "  SUM(CASE WHEN shares IS NULL THEN 1 ELSE 0 END) "
                "    AS unquantified_count "
                "FROM live.shareholder_holdings "
                "GROUP BY ins_code, date")
            # Quantified rows resolved to their canonical holder (step 1).
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_quant AS "
                "SELECT h.ins_code AS ins_code, h.date AS date, "
                "  CASE WHEN x.status IN ('verified_same_source_row', "
                "    'inferred_exact_match') THEN x.canonical_scheme "
                "    ELSE h.id_scheme END AS canon_scheme, "
                "  CASE WHEN x.status IN ('verified_same_source_row', "
                "    'inferred_exact_match') THEN x.canonical_id "
                "    ELSE h.shareholder_id END AS canon_id, "
                "  h.id_scheme AS id_scheme, "
                "  h.shareholder_id AS shareholder_id, "
                "  h.shares AS shares, "
                "  h.identity_status AS ist, "
                "  COALESCE(s.name_normalized, s.name, '') AS nm "
                "FROM live.shareholder_holdings h "
                "LEFT JOIN live.shareholder_id_xref x "
                "  ON x.ins_code = h.ins_code "
                " AND x.source_scheme = h.id_scheme "
                " AND x.source_id = h.shareholder_id "
                "LEFT JOIN live.shareholders s "
                "  ON s.id_scheme = h.id_scheme "
                " AND s.shareholder_id = h.shareholder_id "
                "WHERE h.shares IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM site_excluded_holder_rows x "
                " WHERE x.ins_code=h.ins_code AND x.date=h.date "
                " AND x.id_scheme=h.id_scheme "
                " AND x.shareholder_id=h.shareholder_id)")
            sidecar.execute(
                "CREATE INDEX tmp_dedup_quant_key "
                "ON tmp_dedup_quant(ins_code, date, shares)")
            # Provable-majority twins (step 2): unlinked ondate <-> share
            # pairs, same (ins, date, shares, name), exactly one
            # candidate on each side, shares a strict majority of the
            # same-date total. Keyed to the share_id side.
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_twins AS "
                "SELECT q1.ins_code AS ins_code, q1.date AS date, "
                "  q1.shareholder_id AS ondate_id, "
                "  q2.shareholder_id AS share_id "
                "FROM tmp_dedup_quant q1 "
                "JOIN tmp_dedup_quant q2 "
                "  ON q2.ins_code = q1.ins_code AND q2.date = q1.date "
                " AND q2.shares = q1.shares "
                " AND q1.id_scheme = 'ondate_share_id' "
                " AND q2.id_scheme = 'share_id' "
                " AND q1.canon_scheme = q1.id_scheme "
                " AND q1.canon_id = q1.shareholder_id "
                " AND q2.canon_scheme = q2.id_scheme "
                " AND q2.canon_id = q2.shareholder_id "
                " AND q1.nm = q2.nm "
                " AND q1.shares * 2 > (SELECT total_shares "
                "   FROM tmp_dedup_totals t "
                "   WHERE t.ins_code = q1.ins_code AND t.date = q1.date) "
                " AND NOT EXISTS (SELECT 1 FROM tmp_dedup_quant q3 "
                "   WHERE q3.ins_code = q1.ins_code AND q3.date = q1.date "
                "   AND q3.shares = q1.shares "
                "   AND q3.id_scheme = 'ondate_share_id' "
                "   AND q3.canon_scheme = q3.id_scheme "
                "   AND q3.canon_id = q3.shareholder_id "
                "   AND q3.shareholder_id <> q1.shareholder_id) "
                " AND NOT EXISTS (SELECT 1 FROM tmp_dedup_quant q4 "
                "   WHERE q4.ins_code = q1.ins_code AND q4.date = q1.date "
                "   AND q4.shares = q1.shares "
                "   AND q4.id_scheme = 'share_id' "
                "   AND q4.canon_scheme = q4.id_scheme "
                "   AND q4.canon_id = q4.shareholder_id "
                "   AND q4.shareholder_id <> q2.shareholder_id)")
            # Final holder per quantified row: twin members collapse to
            # the share_id side, the rest keep their canonical id.
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_final AS "
                "SELECT q.ins_code AS ins_code, q.date AS date, "
                "  CASE WHEN t.share_id IS NOT NULL THEN 'share_id' "
                "    ELSE q.canon_scheme END AS final_scheme, "
                "  CASE WHEN t.share_id IS NOT NULL THEN t.share_id "
                "    ELSE q.canon_id END AS final_id, "
                "  q.shares AS shares, q.nm AS nm, "
                "  CASE q.ist "
                "    WHEN 'verified_same_source_row' THEN 4 "
                "    WHEN 'inferred_exact_match' THEN 3 "
                "    WHEN 'source_id' THEN 2 "
                "    WHEN 'unlinked_record_id' THEN 1 "
                "    WHEN 'ambiguous_not_linked' THEN 1 "
                "    ELSE 0 END AS ist_rank "
                "FROM tmp_dedup_quant q "
                "LEFT JOIN tmp_dedup_twins t "
                "  ON t.ins_code = q.ins_code AND t.date = q.date "
                " AND ((q.id_scheme = 'ondate_share_id' "
                "      AND q.shareholder_id = t.ondate_id) "
                "   OR (q.id_scheme = 'share_id' "
                "      AND q.shareholder_id = t.share_id))")
            # Best identity tier per resolved holder: the group's name
            # comes from its best-tier member (same ranking as the
            # Python resolver: verified 4, inferred 3, source_id 2,
            # unlinked/ambiguous 1, unknown 0).
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_holder_rank AS "
                "SELECT ins_code, date, final_scheme, final_id, "
                "  MAX(ist_rank) AS max_rank "
                "FROM tmp_dedup_final "
                "GROUP BY ins_code, date, final_scheme, final_id")
            # One row per resolved holder (MAX shares: twin members carry
            # the same shares by construction; a canonical with two
            # disagreeing rows is a pipeline conflict and the larger
            # value is kept -- see the report).
            # The name comes from the best-tier member (ist_rank =
            # max_rank); a tie is broken by the largest name, exactly as
            # the Python resolver does -- so both paths name the group
            # identically (D1 fix).
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_holders AS "
                "SELECT f.ins_code AS ins_code, f.date AS date, "
                "  f.final_scheme AS final_scheme, "
                "  f.final_id AS final_id, "
                "  MAX(f.shares) AS shares, MAX(f.nm) AS nm "
                "FROM tmp_dedup_final f "
                "JOIN tmp_dedup_holder_rank r "
                "  ON r.ins_code = f.ins_code AND r.date = f.date "
                " AND r.final_scheme = f.final_scheme "
                " AND r.final_id = f.final_id "
                " AND f.ist_rank = r.max_rank "
                "GROUP BY f.ins_code, f.date, f.final_scheme, f.final_id")
            sidecar.execute(
                "CREATE INDEX tmp_dedup_holders_key "
                "ON tmp_dedup_holders(ins_code, date, shares, nm)")
            # Unresolved duplicate candidates (step 3): distinct resolved
            # holders sharing (date, shares, name) with no link between
            # them. They stay separate holders, but the day is 'review'
            # because the total may double-count one person.
            sidecar.execute(
                "CREATE TEMP TABLE tmp_dedup_unres AS "
                "SELECT g.ins_code AS ins_code, g.date AS date, "
                "  COUNT(*) AS unresolved_count, "
                "  COALESCE(SUM(g.shares), 0) AS unresolved_shares "
                "FROM tmp_dedup_holders g "
                "JOIN (SELECT ins_code, date, shares, nm "
                "      FROM tmp_dedup_holders "
                "      GROUP BY ins_code, date, shares, nm "
                "      HAVING COUNT(*) > 1) d "
                "  ON d.ins_code = g.ins_code AND d.date = g.date "
                " AND d.shares = g.shares AND d.nm = g.nm "
                "GROUP BY g.ins_code, g.date")
            with sidecar:
                sidecar.execute(
                    "INSERT INTO site_company_daily_dedup "
                    "(ins_code, date, resolved_count, resolved_shares, "
                    " resolved_pct, raw_count, raw_pct, raw_shares, "
                    " unquantified_count, unresolved_count, "
                    " unresolved_shares, total_shares, status, status_note) "
                    "SELECT r.ins_code, r.date, "
                    "  COALESCE(h.resolved_count, 0), "
                    "  COALESCE(h.resolved_shares, 0), "
                    "  CASE WHEN t.total_shares IS NULL "
                    "    OR t.total_shares = 0 THEN NULL "
                    "    ELSE ROUND(COALESCE(h.resolved_shares, 0) * 100.0 "
                    "      / t.total_shares, 6) END, "
                    "  r.raw_count, r.raw_pct, r.raw_shares, "
                    "  r.unquantified_count, "
                    "  COALESCE(u.unresolved_count, 0), "
                    "  COALESCE(u.unresolved_shares, 0), "
                    "  t.total_shares, "
                    "  CASE WHEN t.total_shares IS NULL "
                    "    OR t.total_shares = 0 THEN 'unavailable' "
                    "    WHEN COALESCE(u.unresolved_count, 0) > 0 "
                    "    THEN 'review' "
                    "    WHEN COALESCE(h.resolved_shares, 0) * 100.0 "
                    "      / t.total_shares > 100 THEN 'review' "
                    "    ELSE 'ok' END, "
                    "  CASE WHEN t.total_shares IS NULL "
                    "    OR t.total_shares = 0 "
                    "    THEN 'کل سهام شرکت برای این تاریخ ثبت نشده؛ "
                    "درصد محاسبه نشد.' "
                    "    WHEN COALESCE(u.unresolved_count, 0) > 0 "
                    "    THEN 'رکوردهای تکراری حل‌نشده با سهام و نام "
                    "یکسان وجود دارد؛ جمع ممکن است مضاعف باشد.' "
                    "    WHEN COALESCE(h.resolved_shares, 0) * 100.0 "
                    "      / t.total_shares > 100 "
                    "    THEN 'جمع مالکیت حل‌شده بیش از ۱۰۰٪ است؛ "
                    "نیاز به بازبینی دارد.' "
                    "    ELSE NULL END "
                    "FROM tmp_dedup_raw r "
                    "LEFT JOIN tmp_dedup_totals t "
                    "  ON t.ins_code = r.ins_code AND t.date = r.date "
                    "LEFT JOIN (SELECT ins_code, date, COUNT(*) "
                    "    AS resolved_count, "
                    "    COALESCE(SUM(shares), 0) AS resolved_shares "
                    "  FROM tmp_dedup_holders "
                    "  GROUP BY ins_code, date) h "
                    "  ON h.ins_code = r.ins_code AND h.date = r.date "
                    "LEFT JOIN tmp_dedup_unres u "
                    "  ON u.ins_code = r.ins_code AND u.date = r.date")
            n_rows = sidecar.execute(
                "SELECT COUNT(*) FROM site_company_daily_dedup").fetchone()[0]
            n_review, n_unavail = sidecar.execute(
                "SELECT SUM(CASE WHEN status='review' THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN status='unavailable' THEN 1 ELSE 0 END) "
                "FROM site_company_daily_dedup").fetchone()
            print("[refresh] dedup: %d (symbol,date) rows, %d review, "
                  "%d unavailable in %.1fs"
                  % (n_rows, n_review or 0, n_unavail or 0,
                     time.time() - t8), flush=True)
            ownership_resolver_ok = True
        finally:
            sidecar.execute("DETACH DATABASE live")
            for _tmp in ("tmp_dedup_totals", "tmp_dedup_raw",
                         "tmp_dedup_quant", "tmp_dedup_twins",
                         "tmp_dedup_final", "tmp_dedup_holder_rank",
                         "tmp_dedup_holders",
                         "tmp_dedup_unres"):
                sidecar.execute("DROP TABLE IF EXISTS " + _tmp)
    except Exception as e:
        print("[refresh] dedup skipped: %r" % (e,), flush=True)
    ownership_watch_enrich_ok = False
    # 9. Enrich site_watch with the identity-resolved latest snapshot,
    #    the latest price, and the market value. Runs after step 8 so the
    #    resolved table exists. The price comes from trading_days (latest
    #    closing price): the price column of shareholder_holdings is NULL
    #    on the latest date for 573 of 608 symbols on the real DB, so
    #    using it left market_value NULL almost everywhere. The
    #    total-holder-count fallback: the pipeline often leaves
    #    company_daily.total_shareholder_count NULL; then the resolved
    #    disclosed count is shown as a lower bound ("at least N major
    #    holders") with a note, instead of "نامشخص (NULL)".
    try:
        t9 = time.time()
        latest_price = {}
        for r in cur.execute(
                "SELECT t.ins_code AS ins_code, t.p_closing AS price, "
                "  t.date AS d FROM trading_days t "
                "JOIN (SELECT ins_code, MAX(date) AS md FROM trading_days "
                "      WHERE p_closing IS NOT NULL AND p_closing > 0 "
                "      GROUP BY ins_code) m "
                "  ON m.ins_code = t.ins_code AND t.date = m.md").fetchall():
            latest_price[r["ins_code"]] = (r["price"], r["d"])
        updates = []
        for w in sidecar.execute(
                "SELECT ins_code, latest_date, total_company_shares, "
                "  total_shareholder_count, disclosed_shareholder_count, "
                "  disclosed_ownership_percent_sum FROM site_watch").fetchall():
            d = sidecar.execute(
                "SELECT resolved_count, resolved_pct, unresolved_count, "
                "  status, status_note FROM "
                "  site_company_daily_dedup WHERE ins_code=? AND date=?",
                (w["ins_code"], w["latest_date"])).fetchone()
            price, pdate = latest_price.get(w["ins_code"], (None, None))
            mv = None
            if price is not None and w["total_company_shares"]:
                mv = w["total_company_shares"] * price
            total_n = w["total_shareholder_count"]
            note = None
            if total_n is None and d is not None:
                total_n = d["resolved_count"]
                note = ("تعداد کل سهام‌داران در داده ثبت نشده؛ این عدد "
                        "تعداد سهام‌داران عمدهٔ یک‌تا‌شده است (حداقل).")
            updates.append((
                d["resolved_count"] if d else None,
                d["resolved_pct"] if d else None,
                d["unresolved_count"] if d else 0,
                d["status"] if d else None,
                d["status_note"] if d else None,
                w["disclosed_ownership_percent_sum"],
                price, _norm_date(pdate) if pdate else None, mv,
                total_n, note, w["ins_code"]))
        with sidecar:
            sidecar.executemany(
                "UPDATE site_watch SET resolved_shareholder_count=?, "
                "  resolved_ownership_percent_sum=?, "
                "  resolved_unresolved_count=?, resolved_status=?, "
                "  resolved_status_note=?, "
                "  raw_ownership_percent_sum=?, latest_price=?, "
                "  latest_price_date=?, market_value=?, "
                "  total_shareholder_count=?, "
                "  total_shareholder_count_note=? WHERE ins_code=?",
                updates)
        print("[refresh] watch enriched: %d symbols in %.1fs"
              % (len(updates), time.time() - t9), flush=True)
        ownership_watch_enrich_ok = True
    except Exception as e:
        print("[refresh] watch enrich skipped: %r" % (e,), flush=True)
    # Sidecar layout version, so the next start forces a rebuild when the
    # layout changed even though the live DB file did not.
    with sidecar:
        sidecar.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) "
            "VALUES ('sidecar_version', ?)", (str(SIDECAR_VERSION),))
    # A failed ownership build must never be marked ready or accepted by
    # the unchanged-fingerprint shortcut. The numeric sidecar version stays
    # 7 during this isolated port; this marker distinguishes the new layout.
    if not ownership_resolver_ok or not ownership_watch_enrich_ok:
        with sidecar:
            sidecar.execute(
                "DELETE FROM site_meta WHERE key='ownership_resolver_schema'")
            sidecar.execute(
                "DELETE FROM site_meta WHERE key='last_refresh'")
        raise RuntimeError(
            "ownership resolver or watch enrichment failed; sidecar remains not-ready")
    with sidecar:
        sidecar.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) "
            "VALUES ('ownership_resolver_schema', ?)",
            ("resolved-v1-source-exclusions",))
    # Completion marker, written ONLY after every table and panel is built.
    # require_ready() uses it, so the API answers 503 (not partial data)
    # until the first full refresh has finished.
    with sidecar:
        sidecar.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) VALUES ('last_refresh', ?)",
            (datetime.now().isoformat(timespec="seconds"),))
    print("[refresh] done in %.1fs total" % (time.time() - t0), flush=True)

# ---------------------------------------------------------------------------
# API implementation
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _q_int(params, name, default, lo, hi):
    try:
        v = int(params.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _q_str(params, name, default=""):
    v = params.get(name, [default])
    return v[0] if v else default


def _like(s):
    return "%" + str(s).replace("%", "").replace("_", "") + "%"


class SiteApi:
    """Holds the DB handles and serves every /api/v1/* endpoint."""

    def __init__(self, db_path, sidecar_path, site_dir, output_dir):
        self.db_path = db_path
        self.sidecar_path = sidecar_path
        self.site_dir = site_dir
        self.output_dir = output_dir
        self._overview_cache = {"at": 0.0, "payload": None}
        self._refresh_error = None
        self._refresh_lock = threading.Lock()

    # -- connections -----------------------------------------------------
    def live(self):
        con = open_live_db(self.db_path)
        # The sidecar stores source-proven duplicate rows. Attach it to every
        # read-only live connection so detail, history and portfolio queries
        # can all apply the same exclusions without copying raw DB rows.
        if os.path.isfile(self.sidecar_path):
            attached = {r[1] for r in con.execute("PRAGMA database_list")}
            if "sidecar_data" not in attached:
                con.execute("ATTACH DATABASE ? AS sidecar_data",
                            (self.sidecar_path,))
        return con

    def sidecar(self):
        con = open_sidecar_db(self.sidecar_path)
        con.executescript(SIDECAR_SCHEMA)
        return con

    def meta(self, con, key, default=None):
        row = con.execute(
            "SELECT value FROM site_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def refresh(self):
        # Skip the rebuild when the live DB file has not changed since the
        # last successful refresh: the summaries are already current and a
        # rebuild of the 13.8M-row holdings table is expensive.
        with self._refresh_lock:
            try:
                st = os.stat(self.db_path)
                fingerprint = "%d:%d" % (st.st_size, int(st.st_mtime))
            except OSError:
                fingerprint = None
            sc = self.sidecar()
            try:
                # Ensure the search-ordering index exists on the CURRENT
                # sidecar, even when the fingerprint check below skips the
                # rebuild: the index was added after the first build, and
                # forcing a full 30-minute rebuild just to create it would
                # take the API down for nothing. CREATE INDEX IF NOT
                # EXISTS is a no-op once it is there. (Step 2 creates the
                # same index on fresh rebuilds.)
                if sc.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='site_holder_latest'").fetchone():
                    with sc:
                        sc.execute(
                            "CREATE INDEX IF NOT EXISTS idx_holder_latest_pct "
                            "ON site_holder_latest(latest_date DESC, "
                            "ownership_percent_calc DESC)")
                if fingerprint is not None:
                    have = self.meta(sc, "db_fingerprint")
                    version = self.meta(sc, "sidecar_version")
                    ownership_schema = self.meta(sc, "ownership_resolver_schema")
                    n_watch = sc.execute(
                        "SELECT COUNT(*) FROM site_watch").fetchone()[0]
                    if have == fingerprint and n_watch > 0 and \
                            version == str(SIDECAR_VERSION) and \
                            ownership_schema == "resolved-v1-source-exclusions":
                        print("[refresh] live DB unchanged since last build; "
                              "summaries are current, skipping rebuild.",
                              flush=True)
                        self._refresh_error = None
                        return
                    if version != str(SIDECAR_VERSION):
                        print("[refresh] sidecar layout v%s -> v%s; "
                              "forcing a full rebuild." % (
                                  version, SIDECAR_VERSION), flush=True)
            finally:
                sc.close()
            try:
                live = self.live()
                sc = self.sidecar()
                try:
                    refresh_summaries(live, sc)
                    if fingerprint is not None:
                        with sc:
                            sc.execute(
                                "INSERT OR REPLACE INTO site_meta (key, value) "
                                "VALUES ('db_fingerprint', ?)", (fingerprint,))
                    self._refresh_error = None
                finally:
                    live.close()
                    sc.close()
            except Exception as e:
                self._refresh_error = "%r" % (e,)
                print("[refresh] FAILED: %r" % (e,), flush=True)

    def require_ready(self, sc):
        # Ready only after a full refresh completed AND the expected ownership
        # resolver schema was materialised. This rejects both partial builds
        # and legacy v7 sidecars that still carry the old deduped_* layout.
        row = sc.execute(
            "SELECT value FROM site_meta WHERE key='last_refresh'").fetchone()
        ownership_schema = self.meta(sc, "ownership_resolver_schema")
        if (not row or not row[0]
                or ownership_schema != "resolved-v1-source-exclusions"):
            raise ApiError(503, "داده‌ها هنوز آماده نیستند؛ ساخت کامل Sidecar لازم است."
                            + (" خطا: " + self._refresh_error
                               if self._refresh_error else ""))

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
                gregorian_yyyymmdd_to_jalali(earliest) if earliest else None,
            "latest_date_jalali":
                gregorian_yyyymmdd_to_jalali(latest) if latest else None,
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
            q = _q_str(params, "q").strip()
            market = _q_str(params, "market", "ALL")
            sort_by = _q_str(params, "sort_by", "disclosed_pct")
            sort_dir = _q_str(params, "sort_dir", "DESC").upper()
            limit = _q_int(params, "limit", 30, 1, 200)
            offset = _q_int(params, "offset", 0, 0, 1000000)
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
                frag, fargs = _like_or_clause(
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
            q = _q_str(params, "q").strip()
            limit = _q_int(params, "limit", 8, 1, 50)
            if not q:
                return {"results": []}
            frag, fargs = _like_or_clause(["symbol", "company_name"], q)
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
        norm = normalize_fa(symbol)
        for r in sc.execute("SELECT * FROM site_watch").fetchall():
            if normalize_fa(r["symbol"]) == norm:
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
            d = dedup.get(_norm_date(r["date"]))
            history.append({
                "date_jalali": gregorian_yyyymmdd_to_jalali(r["date"]),
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

    def stock_shareholders(self, symbol, params):
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
        finally:
            sc.close()
        date_jalali = _q_str(params, "date_jalali").strip()
        if date_jalali:
            date_g = jalali_to_gregorian_date(date_jalali)
            if not date_g:
                raise ApiError(400, "تاریخ جلالی نامعتبر است: %s" % date_jalali)
        else:
            date_g = w["latest_date"]
            date_jalali = w["latest_date_jalali"]
        sort_by = _q_str(params, "sort_by", "shares")
        limit = _q_int(params, "limit", 100, 1, 500)
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
            groups = self._resolve_date_rows(
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

    def _resolve_date_rows(self, live, ins_code, date_g, rows):
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
        scheme = _q_str(params, "id_scheme").strip()
        sh_id = _q_str(params, "shareholder_id").strip()
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
                d = dedup.get(_norm_date(r["date"]))
                pct = d["resolved_pct"] if d else None
                if pct is not None:
                    company_series.append({
                        "time": fmt_gregorian_date(r["date"]), "value": pct})
                if d and d["resolved_shares"] is not None:
                    company_shares_series.append({
                        "time": fmt_gregorian_date(r["date"]),
                        "value": d["resolved_shares"]})
            holder_series = []
            ev_where = "h.change_type IN ('new','increase','decrease')"
            ev_args = [w["ins_code"]]
            if scheme and sh_id:
                holder_series = [
                    {"time": fmt_gregorian_date(r["date"]),
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
                    p = _latest_close(live, w["ins_code"], r["date"])
                    if p is not None:
                        val = abs(r["shares_change"]) * p
                events.append({
                    "date_jalali": fmt_jalali_date(r["date_jalali"]),
                    "date_gregorian": fmt_gregorian_date(r["date"]),
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

    def stock_corporate_actions(self, symbol):
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
        finally:
            sc.close()
        live = self.live()
        try:
            rows = live.execute(
                "SELECT date, jalali_date, effective_date, action_type, "
                "  old_total_shares, new_total_shares, ratio, verification_status "
                "FROM corporate_actions WHERE ins_code=? ORDER BY date ASC",
                (w["ins_code"],)).fetchall()
        finally:
            live.close()
        out = []
        for r in rows:
            ratio = r["ratio"]
            if ratio is None and r["old_total_shares"] and r["new_total_shares"]:
                ratio = r["new_total_shares"] / r["old_total_shares"]
            out.append({
                "event_date_gregorian": fmt_gregorian_date(r["date"]),
                "event_date_jalali":
                    fmt_jalali_date(r["jalali_date"])
                    or gregorian_yyyymmdd_to_jalali(r["date"]),
                "effective_date_jalali":
                    gregorian_yyyymmdd_to_jalali(r["effective_date"])
                    if r["effective_date"] else None,
                "action_type": r["action_type"],
                "old_total_shares": r["old_total_shares"],
                "new_total_shares": r["new_total_shares"],
                "capital_increase_ratio": ratio,
                "share_ratio_new_over_old": ratio,
                "verification_status": r["verification_status"],
            })
        return {"corporate_actions": out}

    def stock_data_quality(self, symbol):
        sc = self.sidecar()
        try:
            w = self.resolve_symbol(sc, symbol)
        finally:
            sc.close()
        live = self.live()
        try:
            gaps = [{
                "field": r["endpoint"] or "holdings",
                "date_from": gregorian_yyyymmdd_to_jalali(r["date_from"]),
                "date_to": gregorian_yyyymmdd_to_jalali(r["date_to"]),
                "reason": r["reason"],
            } for r in live.execute(
                "SELECT endpoint, date_from, date_to, reason FROM data_gaps "
                "WHERE ins_code=? ORDER BY date_from ASC",
                (w["ins_code"],)).fetchall()]
            recs = [{
                "category": r["category"],
                "description": r["description"],
                "date_jalali": gregorian_yyyymmdd_to_jalali(r["date"]),
            } for r in live.execute(
                "SELECT category, description, date FROM reconciliation_items "
                "WHERE ins_code=? ORDER BY date DESC LIMIT 30",
                (w["ins_code"],)).fetchall()]
        finally:
            live.close()
        return {"data_gaps": gaps, "reconciliation": recs}

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
                "time": fmt_gregorian_date(r["date"]),
                "date_jalali": gregorian_yyyymmdd_to_jalali(r["date"]),
                "open": o, "high": r["p_max"] or r["p_closing"],
                "low": r["p_min"] or r["p_closing"],
                "close": r["p_closing"],
                "volume": r["volume"],
            })
        return {"prices": out}

    # -- 4. Cross-company shareholder search -------------------------------
    def _search_name_pairs(self, live, q):
        """(scheme, id) pairs whose stored name matches q (both ي/ك
        spellings). One scan of the narrow names table (~5 s on the
        real DB) -- never the 6.25M-row holder table."""
        frag, fargs = _like_or_clause(["name"], q)
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
            frag, fargs = _like_or_clause(["symbol"], symbol)
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
        q = _q_str(params, "q").strip()
        symbol = _q_str(params, "symbol").strip()
        stype = _q_str(params, "shareholder_type", "ALL")
        multi = _q_str(params, "multi_company_only", "false").lower() == "true"
        min_pct = _q_str(params, "min_ownership").strip()
        limit = _q_int(params, "limit", 40, 1, 100)
        offset = _q_int(params, "offset", 0, 0, 1000000)
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
                     gregorian_yyyymmdd_to_jalali(g["first_seen"])
                     if g.get("first_seen") else None),
                 "last_observed_date_jalali": (
                     gregorian_yyyymmdd_to_jalali(g["last_seen"])
                     if g.get("last_seen") else None)}
            out.append(d)
        return {"rows": out}

    def shareholder_observation_history(self, params):
        symbol = _q_str(params, "symbol").strip()
        scheme = _q_str(params, "id_scheme").strip()
        sh_id = _q_str(params, "shareholder_id").strip()
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
            {"time": fmt_gregorian_date(r["date"]),
             "value": r["ownership_percent_calc"]}
            for r in rows if r["ownership_percent_calc"] is not None]}

    # -- 5. Investor portfolios ---------------------------------------------
    def portfolio_investors(self, params):
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            q = _q_str(params, "q").strip()
            limit = _q_int(params, "limit", 30, 1, 100)
            # sort_by=linked (default: most records) or inferred (most
            # exact-match inferred links first -- the investors whose
            # identity is actually evidenced; by record count they are
            # buried, because an inferred holder typically has few rows).
            # Ordering by inferred is served by idx_investors_inferred.
            sort_by = _q_str(params, "sort_by").strip()
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

    def _majority_twins(self, live, sc, scheme, sh_id):
        """Provable-majority twins of (scheme, sh_id): a record under the
        other scheme (ondate_share_id <-> share_id) with the same
        (ins_code, latest_date, shares, name) where shares is a STRICT
        majority of the symbol's total shares. Two distinct holders
        cannot each hold >50% of one company, so the match is a proof,
        not a heuristic.
        The pipeline does NOT link one-date matches for masked persons
        (Stage 3: the >=2-date rule), so this only merges the provable
        subset -- a small stake under two ids stays two people, and an
        ambiguous key (two candidates) is never merged.
        Only 'ondate_share_id' <-> 'share_id' twins are considered;
        legacy schemes were already canonicalised by the pipeline."""
        twins = set()
        if scheme not in ("ondate_share_id", "share_id"):
            return twins
        other = "share_id" if scheme == "ondate_share_id" else "ondate_share_id"
        rows = sc.execute(
            "SELECT ins_code, latest_date, shares FROM site_holder_latest "
            "WHERE id_scheme=? AND shareholder_id=? AND shares IS NOT NULL",
            (scheme, sh_id)).fetchall()
        for r in rows:
            cands = sc.execute(
                "SELECT id_scheme, shareholder_id FROM site_holder_latest "
                "WHERE ins_code=? AND latest_date=? AND shares=? "
                "  AND id_scheme=?",
                (r["ins_code"], r["latest_date"], r["shares"], other)).fetchall()
            if len(cands) != 1:
                continue  # ambiguous or absent: no proof
            t_scheme, t_id = cands[0]["id_scheme"], cands[0]["shareholder_id"]
            n1 = live.execute(
                "SELECT COALESCE(name_normalized, name) AS nm "
                "FROM shareholders WHERE id_scheme=? AND shareholder_id=? "
                "LIMIT 1", (scheme, sh_id)).fetchone()
            n2 = live.execute(
                "SELECT COALESCE(name_normalized, name) AS nm "
                "FROM shareholders WHERE id_scheme=? AND shareholder_id=? "
                "LIMIT 1", (t_scheme, t_id)).fetchone()
            tot = live.execute(
                "SELECT total_shares_current FROM companies "
                "WHERE ins_code=? LIMIT 1", (r["ins_code"],)).fetchone()
            if not n1 or not n2 or not tot:
                continue
            if (n1["nm"] or "") != (n2["nm"] or ""):
                continue
            total = tot["total_shares_current"]
            if total and r["shares"] * 2 > total:
                twins.add((t_scheme, t_id))
        return twins

    def _majority_twin_map(self, live, sc, rows, tbl="site_holder_latest"):
        """Batched provable-majority twins for a page of search rows.
        Returns {(scheme, id): (twin_scheme, twin_id)} where the twin is
        the share_id for an ondate row (and vice versa), under the same
        rule as _majority_twins: same (ins_code, latest_date, shares,
        name), exactly one candidate on each side, shares a strict
        majority of the symbol's total shares. The twin is found in
        site_holder_latest even when it is not on this page.
        `tbl` is the qualified table name: the search endpoint runs
        against the live handle with the sidecar ATTACHed, so it passes
        "sidecar.site_holder_latest"."""
        keys = {}
        for r in rows:
            if r["id_scheme"] not in ("ondate_share_id", "share_id"):
                continue
            if r["shares"] is None or r["latest_date"] is None:
                continue
            keys[(r["ins_code"], r["latest_date"], r["shares"])] = True
        if not keys:
            return {}
        # CISIN siblings: the same security is recorded under several
        # ins_codes (duplicate instruments; Q2 proved 144 multi-ins
        # CISIN groups, 68 with identical totals and holder sets). A
        # twin may sit under a sibling ins_code on the same date with
        # the same shares and name -- e.g. بپويا (ondate, 2026-11-14)
        # vs بپويا3 (share_id, 2026-11-14): same shares, same company
        # total, different instrument records. The proof is unchanged
        # (same date, same shares, same name, strict majority of the
        # same company); only the record differs. The total is checked
        # per candidate ins_code below, so a sibling with a different
        # total cannot pass.
        ins0 = {k[0] for k in keys}
        if ins0:
            ph0 = ",".join("?" for _ in ins0)
            cisin_of = {}
            for rr in live.execute(
                "SELECT ins_code, cisin FROM companies "
                "WHERE ins_code IN (%s)" % ph0, list(ins0)).fetchall():
                if rr["cisin"]:
                    cisin_of[rr["ins_code"]] = rr["cisin"]
            if cisin_of:
                cisins = set(cisin_of.values())
                phc = ",".join("?" for _ in cisins)
                sib = {}
                for rr in live.execute(
                    "SELECT ins_code, cisin FROM companies "
                    "WHERE cisin IN (%s)" % phc, list(cisins)).fetchall():
                    sib.setdefault(rr["cisin"], []).append(rr["ins_code"])
                extra = {}
                for (ins_code, d, shares) in list(keys):
                    c = cisin_of.get(ins_code)
                    if not c:
                        continue
                    for sib_ins in sib.get(c, []):
                        if sib_ins != ins_code:
                            extra[(sib_ins, d, shares)] = True
                keys.update(extra)
        ph = ",".join("(?,?,?)" for _ in keys)
        qargs = [v for k in keys for v in k]
        found = sc.execute(
            "SELECT id_scheme, shareholder_id, ins_code, latest_date, shares "
            "FROM %s "
            "WHERE (ins_code, latest_date, shares) IN (%s) "
            "  AND id_scheme IN ('ondate_share_id','share_id')" % (tbl, ph),
            qargs).fetchall()
        by_key = {}
        for f in found:
            by_key.setdefault(
                (f["ins_code"], f["latest_date"], f["shares"]), []).append(
                (f["id_scheme"], f["shareholder_id"]))
        cand_ids = {(s, i) for lst in by_key.values() for (s, i) in lst}
        names = {}
        if cand_ids:
            ph2 = ",".join("(?,?)" for _ in cand_ids)
            qargs2 = [v for p in cand_ids for v in p]
            for rr in live.execute(
                "SELECT id_scheme, shareholder_id, "
                "  COALESCE(name_normalized, name) AS nm FROM shareholders "
                "WHERE (id_scheme, shareholder_id) IN (%s)" % ph2,
                qargs2).fetchall():
                names[(rr["id_scheme"], rr["shareholder_id"])] = rr["nm"]
        ins_codes = {k[0] for k in by_key}
        totals = {}
        if ins_codes:
            ph3 = ",".join("?" for _ in ins_codes)
            for rr in live.execute(
                "SELECT ins_code, total_shares_current FROM companies "
                "WHERE ins_code IN (%s)" % ph3, list(ins_codes)).fetchall():
                totals[rr["ins_code"]] = rr["total_shares_current"]
        # CISIN per found ins_code, for cross-instrument grouping.
        cisin_of = {}
        if ins_codes:
            ph4 = ",".join("?" for _ in ins_codes)
            for rr in live.execute(
                "SELECT ins_code, cisin FROM companies "
                "WHERE ins_code IN (%s)" % ph4, list(ins_codes)).fetchall():
                if rr["cisin"]:
                    cisin_of[rr["ins_code"]] = rr["cisin"]
        # Group by (date, shares, company): the CISIN when known, else
        # the ins_code itself. Rows under sibling ins_codes of one
        # security land in one group, so a twin under بپويا3 links to
        # its ondate record under بپويا (same date, shares, name). The
        # exactly-one-per-side and majority checks run on the GROUP, so
        # two different holders under sibling records can never merge.
        # Residual risk, as with the same-ins rule: a stale disclosure
        # overlapping a real one (same masked name, same shares, same
        # date) would merge two people -- the date+shares+majority
        # combination makes that a data error, not a routine case.
        by_group = {}
        for (ins_code, d, shares), lst in by_key.items():
            gid = cisin_of.get(ins_code) or ins_code
            g = by_group.setdefault((d, shares, gid),
                                    {"ids": [], "ins": set()})
            g["ids"].extend(lst)
            g["ins"].add(ins_code)
        out = {}
        for (_d, shares, _gid), g in by_group.items():
            lst = g["ids"]
            ondates = [p for p in lst if p[0] == "ondate_share_id"]
            share_ids = [p for p in lst if p[0] == "share_id"]
            if len(ondates) != 1 or len(share_ids) != 1:
                continue
            o, s = ondates[0], share_ids[0]
            if (names.get(o) or "") != (names.get(s) or ""):
                continue
            # Majority of the company total, checked for EVERY
            # instrument record in the group (they should agree; if
            # any disagrees or is unknown, no merge).
            ok = True
            for ins_code in g["ins"]:
                total = totals.get(ins_code)
                if not total or shares * 2 <= total:
                    ok = False
                    break
            if ok:
                out[o] = s
                out[s] = o
        return out

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

    def portfolio_detail(self, investor_id, params):
        try:
            scheme, sh_id = investor_id.split(":", 1)
            sh_id = int(sh_id)
        except (ValueError, AttributeError):
            raise ApiError(400, "شناسه سرمایه‌گذار نامعتبر است.")
        as_of = _q_str(params, "as_of_date").strip()
        as_of_dashed = _norm_date(as_of) if as_of else None
        if as_of_dashed:
            try:
                datetime.strptime(as_of_dashed, "%Y-%m-%d")
            except ValueError:
                raise ApiError(400, "تاریخ مبنا باید YYYY-MM-DD باشد.")
        live = self.live()
        sc = self.sidecar()
        try:
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
                    "observation_date_jalali": fmt_jalali_date(h["jalali_date"]),
                    "shares": h["shares"],
                    "ownership_percent_calculated": same_date_pct,
                    "price": price,
                    "price_date": h.get("close_price_date") or h.get("date"),
                    "price_date_jalali": (
                        gregorian_yyyymmdd_to_jalali(
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
            # Value history: per-date total across the holder's CONFIRMED
            # and PROBABLE symbols, so the chart matches the two valued
            # tiers in the summary (uncertain candidates are excluded).
            # Each symbol's history is read under the id that supplied
            # the position (the twin's id for twin positions).
            per_date = {}
            for ins_code, origin_scheme, origin_id in valued:
                for r in live.execute(
                    "SELECT h.date, h.shares, h.price, "
                    "  (SELECT t.p_closing FROM trading_days t "
                    "   WHERE t.ins_code = h.ins_code AND t.date <= h.date "
                    "     AND t.p_closing IS NOT NULL AND t.p_closing > 0 "
                    "   ORDER BY t.date DESC LIMIT 1) AS close_price "
                    "FROM shareholder_holdings h "
                    "WHERE h.ins_code=? AND h.id_scheme=? "
                    "  AND h.shareholder_id=? "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
                    "    WHERE x.ins_code=h.ins_code AND x.date=h.date "
                    "      AND x.id_scheme=h.id_scheme "
                    "      AND x.shareholder_id=h.shareholder_id) "
                    "ORDER BY h.date ASC",
                    (ins_code, origin_scheme, origin_id)).fetchall():
                    p = r["close_price"]
                    if p is None and r["price"] is not None and r["price"] > 0:
                        p = r["price"]
                    v = (r["shares"] * p
                         if r["shares"] is not None and p is not None
                         else None)
                    if v is not None:
                        per_date[r["date"]] = per_date.get(r["date"], 0) + v
            history_series = [
                {"time": fmt_gregorian_date(d), "combined_value_rial": v}
                for d, v in sorted(per_date.items())]
            reg_status = reg["identity_status"] if reg else None
            evidence = {
                # The pipeline's own verdict for this id, plus the raw
                # facts behind it, so the user can see WHY the identity
                # is (or is not) trusted -- not just a badge.
                "registry_identity_status": reg_status,
                "shareholder_type": reg["shareholder_type"] if reg else None,
                "first_seen_date_jalali":
                    gregorian_yyyymmdd_to_jalali(reg["first_seen_date"])
                    if reg and reg["first_seen_date"] else None,
                "last_seen_date_jalali":
                    gregorian_yyyymmdd_to_jalali(reg["last_seen_date"])
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
            "confirmed_positions": confirmed,
            "probable_positions": probable,
            "uncertain_candidates": uncertain,
        }

    # -- 6. Screener ---------------------------------------------------------
    _SCREEN_FIELDS = {
        "ownership_percent_calculated": ("h.ownership_percent_calc", "num"),
        "disclosed_shareholder_count":
            ("w.resolved_shareholder_count", "num"),
        "disclosed_ownership_percent_sum":
            ("w.resolved_ownership_percent_sum", "num"),
        "shares_change": ("h.shares_change", "num"),
        "change_type": ("h.change_type", "str"),
        "entry_status": ("h.entry_status", "str"),
        "shareholder_type": ("n.shareholder_type", "str"),
        "identity_status": ("h.identity_status", "str"),
        "data_completeness": ("w.data_completeness", "str"),
        "market": ("w.market", "str"),
        "has_corporate_action": ("COALESCE(ca.n, 0)", "num"),
        "date_gregorian": ("h.latest_date", "date"),
    }
    _SCREEN_OPS = {">=", "<=", "=", ">", "<"}

    def _screener_query(self, body, limit):
        logic = str(body.get("logic", "AND")).upper()
        if logic not in ("AND", "OR"):
            logic = "AND"
        conds = body.get("conditions") or []
        sort_by = str(body.get("sort_by", "ownership_percent_calculated"))
        sort_dir = str(body.get("sort_dir", "DESC")).upper()
        if sort_dir not in ("ASC", "DESC"):
            sort_dir = "DESC"
        where_parts = []
        args = []
        for c in conds:
            field = self._SCREEN_FIELDS.get(str(c.get("field", "")))
            op = str(c.get("op", "="))
            if field is None or op not in self._SCREEN_OPS:
                continue
            col, kind = field
            val = c.get("value")
            if kind == "num":
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
            elif kind == "date":
                # Stored sidecar dates are canonical dashed Gregorian.
                val = _norm_date(val)
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    continue
            else:
                val = str(val)
            where_parts.append("%s %s ?" % (col, op))
            args.append(val)
        clause = ""
        if where_parts:
            clause = "WHERE " + (" %s " % logic).join(where_parts)
        sort_col, _ = self._SCREEN_FIELDS.get(
            sort_by, ("h.ownership_percent_calc", "num"))
        sql = (
            "SELECT h.symbol AS symbol, w.market AS market, "
            "  n.name AS shareholder_name, "
            "  h.ownership_percent_calc AS ownership_percent_calculated, "
            "  h.shares_change AS shares_change, "
            "  w.resolved_ownership_percent_sum "
            "    AS disclosed_ownership_percent_sum "
            "FROM site_holder_latest h "
            "JOIN site_watch w ON w.ins_code = h.ins_code "
            "LEFT JOIN site_shareholder_names n "
            "  ON n.id_scheme = h.id_scheme "
            " AND n.shareholder_id = h.shareholder_id "
            "LEFT JOIN site_corp_action_counts ca ON ca.ins_code = h.ins_code "
            + clause +
            " ORDER BY %s %s LIMIT ?" % (sort_col, sort_dir))
        return sql, args, limit

    def screener_query(self, body):
        limit = max(1, min(500, int(body.get("limit", 100) or 100)))
        sql, args, _ = self._screener_query(body, limit)
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            rows = sc.execute(sql, args + [limit]).fetchall()
            return {"rows": [dict(r) for r in rows]}
        finally:
            sc.close()

    def screener_export(self, body):
        limit = 5000
        sql, args, _ = self._screener_query(body, limit)
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            rows = sc.execute(sql, args + [limit]).fetchall()
        finally:
            sc.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["symbol", "market", "shareholder_name",
                    "ownership_percent_calculated", "shares_change",
                    "disclosed_ownership_percent_sum"])
        for r in rows:
            w.writerow([r["symbol"], r["market"], r["shareholder_name"],
                        r["ownership_percent_calculated"], r["shares_change"],
                        r["disclosed_ownership_percent_sum"]])
        return buf.getvalue()

    # -- 7. Saved filters + watchlist (sidecar) ------------------------------
    def saved_filters_list(self):
        sc = self.sidecar()
        try:
            rows = sc.execute(
                "SELECT id, name, filter_ast_json FROM saved_filters "
                "ORDER BY id ASC").fetchall()
            return {"filters": [dict(r) for r in rows]}
        finally:
            sc.close()

    def saved_filters_create(self, body):
        name = str(body.get("name", "")).strip()
        if not name:
            raise ApiError(400, "نام فیلتر لازم است.")
        sc = self.sidecar()
        try:
            with sc:
                cur = sc.execute(
                    "INSERT INTO saved_filters (name, filter_ast_json, created_at) "
                    "VALUES (?,?,?)",
                    (name, json.dumps(body.get("filter_ast") or {},
                                      ensure_ascii=False),
                     datetime.now().isoformat(timespec="seconds")))
            return {"id": cur.lastrowid, "name": name}
        finally:
            sc.close()

    def watchlist_get(self):
        sc = self.sidecar()
        try:
            # LEFT JOIN: a symbol added from the companies table may have
            # no daily snapshot yet; it must still show (with '—' stats),
            # not silently vanish from the list.
            rows = sc.execute(
                "SELECT wl.symbol AS symbol, w.company_name AS company_name, "
                "  w.market AS market, "
                "  w.disclosed_shareholder_count AS disclosed_shareholder_count, "
                "  w.resolved_shareholder_count AS resolved_shareholder_count, "
                "  w.disclosed_ownership_percent_sum AS disclosed_ownership_percent_sum, "
                "  w.resolved_ownership_percent_sum AS resolved_ownership_percent_sum, "
                "  w.market_value AS market_value, "
                "  w.latest_price AS latest_price "
                "FROM watchlist wl LEFT JOIN site_watch w "
                "  ON w.symbol = wl.symbol "
                "ORDER BY wl.added_at ASC").fetchall()
            return {"items": [dict(r) for r in rows]}
        finally:
            sc.close()

    def watchlist_add(self, body):
        symbol = str(body.get("symbol", "")).strip()
        if not symbol:
            raise ApiError(400, "نماد لازم است.")
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            row = self._resolve_symbol_row(sc, symbol)
            if row is None:
                # Not in the daily snapshots (116 companies have no
                # company_daily rows yet): accept it if the pipeline
                # knows the company at all, so the add never fails for
                # a real symbol.
                live = self.live()
                try:
                    comp = live.execute(
                        "SELECT ins_code, symbol, name FROM companies "
                        "WHERE symbol=?", (symbol,)).fetchone()
                    if comp is None:
                        norm = normalize_fa(symbol)
                        for c in live.execute(
                                "SELECT ins_code, symbol, name FROM companies"
                                ).fetchall():
                            if normalize_fa(c["symbol"]) == norm:
                                comp = c
                                break
                    if comp is None:
                        raise ApiError(404, "نماد یافت نشد: %s" % symbol)
                    symbol = comp["symbol"]
                finally:
                    live.close()
            else:
                symbol = row["symbol"]
            with sc:
                sc.execute(
                    "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?,?)",
                    (symbol, datetime.now().isoformat(timespec="seconds")))
            return {"ok": True}
        finally:
            sc.close()

    def watchlist_remove(self, symbol):
        sc = self.sidecar()
        try:
            with sc:
                sc.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
            return {"ok": True}
        finally:
            sc.close()

    # -- 8. Admin --------------------------------------------------------------
    def _output_workbooks(self):
        """(file_name, symbol, ins_code, is_temp) for workbooks on disk."""
        out = []
        if not os.path.isdir(self.output_dir):
            return out
        for name in sorted(os.listdir(self.output_dir)):
            if not name.endswith(".xlsx"):
                continue
            if name == "instrument_classification.xlsx":
                continue
            m = re.match(r"^(.*)_(\d+)\.xlsx$", name)
            if m:
                out.append((name, m.group(1), m.group(2), name.startswith(".")))
            else:
                # Orphaned atomic-export temp file:
                # .{symbol}_{ins}.tmp-{pid}-{uuid}.xlsx
                mt = re.match(r"^\.(.*)_(\d+)\.tmp-[^.]+\.xlsx$", name)
                if mt:
                    out.append((name, mt.group(1), mt.group(2), True))
                else:
                    out.append((name, None, None, name.startswith(".")))
        return out

    def admin_overview(self):
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            unique_symbols = sc.execute(
                "SELECT COUNT(*) FROM site_watch").fetchone()[0]
            holdings = self.meta(sc, "holdings_count")
            last_ts = self.meta(sc, "last_fetch_ts")
        finally:
            sc.close()
        books = self._output_workbooks()
        completed = sum(1 for b in books if not b[3])
        failed = sum(1 for b in books if b[3])
        return {
            "manifests_by_status": {"completed": completed, "failed": failed,
                                    "pending": 0, "processing": 0},
            "totals": {"total_discovered_files": len(books),
                       "last_successful_ingestion": last_ts},
            "sqlite_health": {"healthy": True,
                              "unique_symbols": unique_symbols,
                              "holdings_records": int(holdings) if holdings else 0},
            "latest_run": None,
            "ingestion_subprocess_running": False,
        }

    def admin_manifests(self, params):
        status = _q_str(params, "status", "ALL")
        q = _q_str(params, "q").strip()
        limit = _q_int(params, "limit", 50, 1, 200)
        sc = self.sidecar()
        try:
            counts = dict(sc.execute(
                "SELECT ins_code, holdings_rows FROM site_symbol_holdings"
                ).fetchall())
        finally:
            sc.close()
        rows = []
        for name, symbol, ins, is_temp in self._output_workbooks():
            st = "failed" if is_temp else "completed"
            if status != "ALL" and status != st:
                continue
            if q and q not in name and (symbol is None or q not in symbol):
                continue
            rows.append({
                "file_name": name,
                "symbol": symbol,
                "holdings_rows": counts.get(ins) if ins else None,
                "last_stage": "export",
                "status": st,
                "last_error": ("فایل موقت ناتمام از یک خروجی نیمه‌کاره است."
                               if is_temp else None),
            })
            if len(rows) >= limit:
                break
        return {"rows": rows}

    def admin_errors(self, params):
        limit = _q_int(params, "limit", 30, 1, 200)
        live = self.live()
        try:
            rows = live.execute(
                "SELECT id, ins_code, symbol, code, message FROM validation_issues "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            live.close()
        return {"errors": [{
            "id": r["id"],
            "file_name": ("%s_%s.xlsx" % (r["symbol"], r["ins_code"])
                          if r["symbol"] else None),
            "sheet_name": r["code"],
            "row_number": None,
            "error_message": r["message"],
        } for r in rows]}

    def admin_identity_queue(self, params):
        limit = _q_int(params, "limit", 30, 1, 200)
        live = self.live()
        try:
            rows = live.execute(
                "SELECT ins_code, scheme_a, id_a, scheme_b, id_b, evidence, status "
                "FROM identity_link_candidates WHERE status IN "
                "('unresolved','review') ORDER BY ins_code LIMIT ?",
                (limit,)).fetchall()
        finally:
            live.close()
        return {"queue": [dict(r) for r in rows]}

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

# The live database, as the default --db. Written with \u escapes so this
# file stays ASCII.
DEFAULT_DB = ("D:\\\u0628\u06a9\u0627\u067e \u0627\u0648\u0644 "
              "\u0627\u06a9\u0627\u0646\u062a \u06cc\u06a9\\tsetmc_holdings.sqlite")
DEFAULT_OUTPUT = "E:\\tsetmc_holdings\\output"


def _read_json_body(handler, limit=1024 * 1024):
    length = int(handler.headers.get("Content-Length") or 0)
    if length > limit:
        raise ApiError(413, "بدنه درخواست بیش از حد بزرگ است.")
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "بدنه JSON نامعتبر است.")


class Handler(BaseHTTPRequestHandler):
    api = None  # set in main()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep stdout for our own concise request log

    # -- response helpers -------------------------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text, content_type):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_file(self, path):
        site_dir = os.path.abspath(self.api.site_dir)
        target = os.path.abspath(os.path.join(site_dir, path))
        if not target.startswith(site_dir + os.sep) and target != site_dir:
            self._send_json(403, {"detail": "دسترسی مجاز نیست."})
            return
        if os.path.isdir(target):
            target = os.path.join(target, "ai_studio_code.html")
        if not os.path.isfile(target):
            self._send_json(404, {"detail": "فایل یافت نشد."})
            return
        ctype = "text/html; charset=utf-8"
        if target.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif target.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        with open(target, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- routing ------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        t0 = time.time()
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)
        status = 200
        try:
            if path in ("/", "/index.html", "/ai_studio_code.html"):
                self._serve_file("ai_studio_code.html")
                status = 200
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not path.startswith("/api/v1/"):
                self._serve_file(path.lstrip("/"))
                return
            body = _read_json_body(self) if method in ("POST",) else {}
            payload, status, ctype = self._dispatch(method, path, params, body)
            if ctype == "csv":
                self._send_text(200, payload,
                                "text/csv; charset=utf-8")
                print("%s %s -> 200 csv %.2fs" % (method, path, time.time() - t0),
                      flush=True)
                return
            self._send_json(status, payload)
        except ApiError as e:
            status = e.status
            self._send_json(e.status, {"detail": e.detail})
        except Exception as e:
            status = 500
            print("[error] %s %s: %r" % (method, path, e), flush=True)
            try:
                self._send_json(500, {"detail": "خطای داخلی سرور: %s" % e})
            except Exception:
                pass
        finally:
            print("%s %s -> %d %.2fs" % (method, path, status, time.time() - t0),
                  flush=True)

    def _dispatch(self, method, path, params, body):
        api = self.api
        seg = [s for s in path.split("/") if s]
        # seg: ['api','v1', ...]
        rest = seg[2:]
        if method == "GET" and rest == ["market", "overview"]:
            return api.market_overview(), 200, "json"
        if method == "GET" and rest == ["market", "watch"]:
            return api.market_watch(params), 200, "json"
        if method == "GET" and rest == ["stocks", "search"]:
            return api.stocks_search(params), 200, "json"
        if method == "GET" and len(rest) == 2 and rest[0] == "stocks":
            return api.stock_detail(rest[1]), 200, "json"
        if method == "GET" and len(rest) == 3 and rest[0] == "stocks":
            if rest[2] == "shareholders":
                return api.stock_shareholders(rest[1], params), 200, "json"
            if rest[2] == "ownership-series":
                return api.stock_ownership_series(rest[1], params), 200, "json"
            if rest[2] == "corporate-actions":
                return api.stock_corporate_actions(rest[1]), 200, "json"
            if rest[2] == "data-quality":
                return api.stock_data_quality(rest[1]), 200, "json"
            if rest[2] == "prices":
                return api.stock_prices(rest[1]), 200, "json"
        if method == "GET" and rest == ["shareholders", "search"]:
            return api.shareholders_search(params), 200, "json"
        if method == "GET" and rest == ["shareholders", "observation-history"]:
            return api.shareholder_observation_history(params), 200, "json"
        if method == "GET" and rest == ["portfolios", "investors"]:
            return api.portfolio_investors(params), 200, "json"
        if method == "GET" and len(rest) == 2 and rest[0] == "portfolios":
            return api.portfolio_detail(rest[1], params), 200, "json"
        if method == "POST" and rest == ["screener", "query"]:
            return api.screener_query(body), 200, "json"
        if method == "POST" and rest == ["screener", "export"]:
            return api.screener_export(body), 200, "csv"
        if method == "GET" and rest == ["screener", "saved-filters"]:
            return api.saved_filters_list(), 200, "json"
        if method == "POST" and rest == ["screener", "saved-filters"]:
            return api.saved_filters_create(body), 200, "json"
        if method == "GET" and rest == ["watchlists", "default"]:
            return api.watchlist_get(), 200, "json"
        if method == "POST" and rest == ["watchlists", "default", "symbols"]:
            return api.watchlist_add(body), 200, "json"
        if method == "DELETE" and len(rest) == 4 and rest[:3] == [
                "watchlists", "default", "symbols"]:
            return api.watchlist_remove(rest[3]), 200, "json"
        if method == "GET" and rest == ["admin", "overview"]:
            return api.admin_overview(), 200, "json"
        if method == "GET" and rest == ["admin", "manifests"]:
            return api.admin_manifests(params), 200, "json"
        if method == "GET" and rest == ["admin", "errors"]:
            return api.admin_errors(params), 200, "json"
        if method == "GET" and rest == ["admin", "identity", "queue"]:
            return api.admin_identity_queue(params), 200, "json"
        if method == "POST" and rest == ["admin", "ingestion", "trigger"]:
            raise ApiError(
                501, "اجرای ایمپورت از پنل مدیریت وصل نشده است. "
                     "پایپلاین جمع‌آوری فقط با دستور مستقیم و با تأیید جداگانه اجرا می‌شود.")
        if method == "POST" and rest == ["admin", "identity", "resolve"]:
            raise ApiError(
                501, "تأیید هویت از پنل مدیریت وصل نشده است؛ "
                     "در حال حاضر هیچ لینک هویتی در انتظار تأیید نیست.")
        raise ApiError(404, "مسیر API یافت نشد: %s %s" % (method, path))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    # The DB path contains Persian characters; the Windows console default
    # (cp1252) cannot print them, which used to crash startup before the
    # first refresh. Force UTF-8 on both streams.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(
        description="API backend for the tsetmc_site dashboard.")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help="path to the tsetmc_holdings SQLite database (read-only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--site-dir", default=here,
                    help="folder holding ai_studio_code.html")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT,
                    help="folder holding the Excel workbooks")
    ap.add_argument("--refresh-minutes", type=float, default=10,
                    help="how often the summary tables are rebuilt (0 = once)")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print("ERROR: database not found: %s" % args.db)
        raise SystemExit(2)
    if not os.path.isfile(os.path.join(args.site_dir, "ai_studio_code.html")):
        print("ERROR: ai_studio_code.html not found in %s" % args.site_dir)
        raise SystemExit(2)

    api = SiteApi(args.db,
                  os.path.join(args.site_dir, "site_data.sqlite"),
                  args.site_dir, args.output_dir)
    Handler.api = api

    print("tsetmc_site API server")
    print("  db:     %s (read-only)" % args.db)
    print("  site:   %s" % args.site_dir)
    print("  output: %s" % args.output_dir)
    try:
        live = api.live()
        try:
            _verify_jalali(live)
        finally:
            live.close()
    except Exception as e:
        print("WARNING: could not open the live DB: %r" % (e,))

    print("building the first summary in the background (on the first run "
          "this scans the holdings table once and can take a while; later "
          "starts reuse it instantly when the database has not changed) ...",
          flush=True)
    threading.Thread(target=api.refresh, daemon=True).start()

    if args.refresh_minutes and args.refresh_minutes > 0:
        def _loop():
            while True:
                time.sleep(args.refresh_minutes * 60)
                api.refresh()
        threading.Thread(target=_loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("listening on http://%s:%d/ -- open it in the browser" % (args.host, args.port),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")


if __name__ == "__main__":
    main()