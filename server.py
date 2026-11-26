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
#   GET  /api/v1/portfolios/<id>/history?ins_code&as_of_date&limit&offset
#   GET  /api/v1/portfolios/<id>?as_of_date
#   POST /api/v1/screener/query        POST /api/v1/screener/export (XLSX)
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
import heapq
import itertools
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime, timedelta

from sector_affinity import SectorAffinityAccumulator, load_sector_reference
from site_api.dispatch import dispatch_api
from site_api.errors import ApiError
from site_api.market_service import MarketService
from site_api.ownership_service import OwnershipService, resolve_date_rows
from site_api.portfolio_service import PortfolioService
from site_api.process_lock import LockAlreadyHeldError, ProcessLock
from site_api.refresh_runtime import start_refresh_runtime
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
    """'1405-08-27', '14050827' or '1405/08/27' -> '2026-11-18'.

    Reject out-of-range Jalali fields before conversion. The arithmetic
    converter itself maps overflow days into later months, which is useful
    for calendar arithmetic but is not valid input for an API date filter.
    """
    s = str(jalali or "").replace("-", "").replace("/", "")
    if len(s) != 8 or not s.isdigit():
        return ""
    jy, jm, jd = int(s[0:4]), int(s[4:6]), int(s[6:8])
    if jy < 1 or not 1 <= jm <= 12:
        return ""
    if jm <= 6:
        max_day = 31
    elif jm <= 11:
        max_day = 30
    else:
        max_day = 30 if _jalali_is_leap(jy) else 29
    if not 1 <= jd <= max_day:
        return ""
    try:
        gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
        return "%04d-%02d-%02d" % (gy, gm, gd)
    except (TypeError, ValueError, OverflowError):
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

def ownership_percent_for_date(shares, total_shares):
    """Rebuild ownership from shares and the company's total on the SAME date.

    Stored ownership_percent_calc is treated as a legacy/cache field only:
    older builds may have used a stale denominator. Never substitute a
    current or neighbouring total. Missing/non-positive denominators stay NULL.
    """
    if shares is None or total_shares is None:
        return None
    try:
        shares_n = int(shares)
        total_n = int(total_shares)
    except (TypeError, ValueError, OverflowError):
        return None
    if shares_n < 0 or total_n <= 0:
        return None
    return round((shares_n * 100.0) / total_n, 6)

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

def classify_portfolio_change(row):
    """Classify disclosed quantity changes without inventing purchases or sales.

    A signed shares_change is only a net observed quantity change. Corporate
    actions and first observations take precedence over directional labels.
    """
    if row.get("identity_conflict"):
        return "identity_conflict"
    if row.get("corporate_action") and row.get("shares") is not None:
        return "capital_action"
    status = row.get("change_calculation_status")
    if status == "not_disclosed_on_date":
        return "not_disclosed"
    if status == "first_observation_no_previous":
        return "first_observation"
    if status == "spans_gap":
        return "change_after_gap"
    delta = row.get("shares_change")
    try:
        delta = int(delta) if delta is not None else None
    except (TypeError, ValueError, OverflowError):
        delta = None
    if delta is not None and delta > 0:
        return "net_increase"
    if delta is not None and delta < 0:
        return "net_decrease"
    if row.get("change_type") == "increase":
        return "increase_unquantified"
    if row.get("change_type") == "decrease":
        return "decrease_unquantified"
    return "observation"


def build_portfolio_history_summary(observations, sector_accumulator=None):
    """Aggregate all historical disclosures while preserving missing quantities.

    The chart sums only positions disclosed with a valid quantity and price on
    that exact date. It is an observed-value series, not a carried-forward NAV.
    """
    by_instrument, daily = {}, {}
    observations_count = known_count = unavailable_count = 0
    identity_conflicts = change_events = cisin_conflicts = 0
    first_date = last_date = None
    event_classes = {
        "identity_conflict", "capital_action", "first_observation",
        "change_after_gap", "net_increase", "net_decrease",
        "increase_unquantified", "decrease_unquantified",
    }
    for row in observations:
        if sector_accumulator is not None:
            sector_accumulator.add(row)
        observations_count += 1
        date, ins_code = row.get("date"), row.get("ins_code")
        symbol = row.get("symbol") or ins_code
        if date and (first_date is None or date < first_date):
            first_date = date
        if date and (last_date is None or date > last_date):
            last_date = date
        state = by_instrument.setdefault(ins_code, {
            "ins_code": ins_code, "symbol": symbol,
            "first_observation_date": date,
            "first_observation_date_jalali": row.get("jalali_date"),
            "last_observation_date": date,
            "last_observation_date_jalali": row.get("jalali_date"),
            "first_observed_shares": None, "last_observation_shares": row.get("shares"),
            "last_known_shares": None, "max_observed_shares": None,
            "min_observed_shares": None, "observations_count": 0,
            "known_shares_observations_count": 0,
            "unavailable_shares_observations_count": 0,
            "increase_events_count": 0, "decrease_events_count": 0,
            "capital_action_events_count": 0, "identity_conflicts_count": 0,
            "net_observed_shares_change": 0,
            "positive_observed_shares_change": 0,
            "negative_observed_shares_change": 0,
            "last_change_type": row.get("change_type"),
            "last_change_calculation_status": row.get("change_calculation_status"),
            "last_reference_price": row.get("reference_price"),
            "last_reference_price_date": row.get("reference_price_date"),
            "last_estimated_holding_value_rial": None, "cisin": row.get("cisin"),
        })
        state["last_observation_date"] = date
        state["last_observation_date_jalali"] = row.get("jalali_date")
        state["last_observation_shares"] = row.get("shares")
        # Never leave a stale value on the most recent date when that date's
        # shares or identity are unavailable.
        state["last_estimated_holding_value_rial"] = None
        state["last_change_type"] = row.get("change_type")
        state["last_change_calculation_status"] = row.get("change_calculation_status")
        state["last_reference_price"] = row.get("reference_price")
        state["last_reference_price_date"] = row.get("reference_price_date")
        state["cisin"] = row.get("cisin") or state.get("cisin")
        state["observations_count"] += 1
        shares, conflict = row.get("shares"), bool(row.get("identity_conflict"))
        if conflict:
            identity_conflicts += 1
            state["identity_conflicts_count"] += 1
        if shares is None or conflict:
            unavailable_count += 1
            state["unavailable_shares_observations_count"] += 1
        else:
            known_count += 1
            state["known_shares_observations_count"] += 1
            if state["first_observed_shares"] is None:
                state["first_observed_shares"] = shares
            state["last_known_shares"] = shares
            state["max_observed_shares"] = (
                shares if state["max_observed_shares"] is None
                else max(state["max_observed_shares"], shares))
            state["min_observed_shares"] = (
                shares if state["min_observed_shares"] is None
                else min(state["min_observed_shares"], shares))
            price = row.get("reference_price")
            state["last_estimated_holding_value_rial"] = (
                shares * price if price is not None and price > 0 else None)
        classification = classify_portfolio_change(row)
        if classification in event_classes:
            change_events += 1
        if classification == "net_increase":
            state["increase_events_count"] += 1
        elif classification == "net_decrease":
            state["decrease_events_count"] += 1
        elif classification == "capital_action":
            state["capital_action_events_count"] += 1
        delta = row.get("shares_change")
        if delta is not None and not conflict:
            try:
                delta = int(delta)
            except (TypeError, ValueError, OverflowError):
                delta = None
            if delta is not None:
                state["net_observed_shares_change"] += delta
                if delta > 0:
                    state["positive_observed_shares_change"] += delta
                elif delta < 0:
                    state["negative_observed_shares_change"] += abs(delta)
        if date:
            date_map = daily.setdefault(date, {})
            key = row.get("cisin") or ins_code
            value = None
            if shares is not None and not conflict:
                price = row.get("reference_price")
                if price is not None and price > 0:
                    value = shares * price
            entry = {"ins_code": ins_code, "shares": None if conflict else shares,
                     "value": value}
            if key not in date_map:
                date_map[key] = entry
            else:
                previous = date_map[key]
                if previous is None:
                    continue
                if previous["shares"] == entry["shares"]:
                    # Prefer the only available valuation when quantities
                    # agree; if both values exist but disagree, do not pick
                    # an arbitrary instrument listing for the economic asset.
                    if previous["value"] is None and entry["value"] is not None:
                        date_map[key] = entry
                        continue
                    if entry["value"] is None or previous["value"] == entry["value"]:
                        continue
                date_map[key] = None
                cisin_conflicts += 1

    if sector_accumulator is not None:
        sector_accumulator.finish()
    historical_symbols = sorted(
        by_instrument.values(),
        key=lambda item: (item["last_observation_date"] or "", item["symbol"] or ""),
        reverse=True)
    for item in historical_symbols:
        item["first_observation_date_jalali"] = (
            fmt_jalali_date(item["first_observation_date_jalali"])
            if item["first_observation_date_jalali"] else None)
        item["last_observation_date_jalali"] = (
            fmt_jalali_date(item["last_observation_date_jalali"])
            if item["last_observation_date_jalali"] else None)
        item["last_reference_price_date_jalali"] = (
            gregorian_yyyymmdd_to_jalali(item["last_reference_price_date"])
            if item["last_reference_price_date"] else None)
    history_series = []
    for date, positions in sorted(daily.items()):
        entries = [item for item in positions.values() if item is not None]
        valued = [item["value"] for item in entries if item["value"] is not None]
        history_series.append({
            "time": fmt_gregorian_date(date),
            "combined_value_rial": sum(valued) if valued else None,
            "observed_instruments_count": len(entries),
            "valued_instruments_count": len(valued),
            "missing_price_or_shares_count": len(entries) - len(valued),
        })
    historical_summary = {
        "symbols_count": len(historical_symbols),
        "observations_count": observations_count,
        "known_shares_observations_count": known_count,
        "unavailable_shares_observations_count": unavailable_count,
        "change_events_count": change_events,
        "identity_conflicts_count": identity_conflicts,
        "cisin_alias_conflicts_count": cisin_conflicts,
        "first_observation_date": first_date,
        "first_observation_date_jalali":
            gregorian_yyyymmdd_to_jalali(first_date) if first_date else None,
        "last_observation_date": last_date,
        "last_observation_date_jalali":
            gregorian_yyyymmdd_to_jalali(last_date) if last_date else None,
        "valuation_method": "same-date disclosed holdings x reference price; no carry-forward",
    }
    return historical_summary, historical_symbols, history_series

def classify_historical_exit(last_holder_date, latest_company_date, last_observed_shares=None):
    """Label absence from a later disclosure as an assumed sale, not a proven trade.

    A shareholder can disappear from a disclosed list by falling below the
    reporting threshold. The UI therefore labels this as an assumption and
    explains the limitation instead of claiming an executed sale.
    """
    if not last_holder_date or not latest_company_date:
        return "unknown"
    if latest_company_date > last_holder_date:
        return "assumed_sold"
    if latest_company_date == last_holder_date:
        return ("currently_disclosed" if last_observed_shares is not None
                else "unknown")
    return "unknown"


# Sidecar layout version. Bumped whenever the refresh builds a new table
# or column: refresh() forces a full rebuild when the stored version
# differs, so a code upgrade never serves a stale sidecar through the
# fingerprint shortcut (the DB file itself has not changed).
SIDECAR_VERSION = 10

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
    # 2b. Materialise every dated holder observation in the rebuildable sidecar.
    #     Optional source columns are introspected so older source DB schemas
    #     remain readable; the source database itself is never modified.
    t_hist = time.time()
    with sidecar:
        sidecar.execute("DROP TABLE IF EXISTS site_holder_history")
        sidecar.execute("""CREATE TABLE site_holder_history (
            id INTEGER PRIMARY KEY,
            id_scheme TEXT NOT NULL, shareholder_id INTEGER NOT NULL,
            ins_code TEXT NOT NULL, symbol TEXT NOT NULL, identity_status TEXT,
            date TEXT NOT NULL, jalali_date TEXT, shares INTEGER,
            ownership_percent_reported REAL, total_company_shares INTEGER,
            price REAL, price_field TEXT, price_date TEXT, valuation_status TEXT,
            market_value REAL, previous_date TEXT, previous_shares INTEGER,
            shares_change INTEGER, shares_change_percent REAL, gap_trading_days INTEGER,
            change_type TEXT, change_calculation_status TEXT, corporate_action TEXT,
            corporate_action_date TEXT, corporate_action_ratio REAL,
            source_change_code INTEGER, source_change_amount REAL, anomaly_flags TEXT,
            data_available INTEGER, note TEXT, entry_status TEXT, source_endpoint TEXT,
            prev_close_price REAL, prev_close_date TEXT, cisin TEXT
        )""")
    history_source_columns = {r[1] for r in cur.execute(
        "PRAGMA table_info(shareholder_holdings)").fetchall()}
    def _history_col(column, fallback="NULL"):
        return "h." + column if column in history_source_columns else fallback
    total_shares_expr = (
        "CASE WHEN cd.ins_code IS NOT NULL THEN cd.total_company_shares "
        "ELSE %s END" % _history_col("total_company_shares"))
    price_expr = _history_col("price")
    history_select = (
        "SELECT h.id_scheme AS id_scheme, h.shareholder_id AS shareholder_id, "
        "h.ins_code AS ins_code, h.symbol AS symbol, "
        "h.identity_status AS identity_status, h.date AS date, "
        "h.jalali_date AS jalali_date, h.shares AS shares, "
        + _history_col("ownership_percent", "NULL") + " AS ownership_percent_reported, "
        + total_shares_expr + " AS total_company_shares, "
        + price_expr + " AS price, "
        + _history_col("price_field") + " AS price_field, "
        + _history_col("price_date", "h.date") + " AS price_date, "
        + _history_col("valuation_status") + " AS valuation_status, "
        + _history_col("market_value") + " AS market_value, "
        + _history_col("previous_date") + " AS previous_date, "
        + _history_col("previous_shares") + " AS previous_shares, "
        + _history_col("shares_change") + " AS shares_change, "
        + _history_col("shares_change_percent") + " AS shares_change_percent, "
        + _history_col("gap_trading_days") + " AS gap_trading_days, "
        + _history_col("change_type") + " AS change_type, "
        + _history_col("change_calculation_status") + " AS change_calculation_status, "
        + _history_col("corporate_action", "ca.action_type") + " AS corporate_action, "
        + _history_col("corporate_action_date", "ca.event_date") + " AS corporate_action_date, "
        + _history_col("corporate_action_ratio", "ca.ratio") + " AS corporate_action_ratio, "
        + _history_col("source_change_code") + " AS source_change_code, "
        + _history_col("source_change_amount") + " AS source_change_amount, "
        + _history_col("anomaly_flags") + " AS anomaly_flags, "
        + _history_col("data_available", "CASE WHEN h.shares IS NULL THEN 0 ELSE 1 END") + " AS data_available, "
        + _history_col("note") + " AS note, "
        + _history_col("entry_status") + " AS entry_status, "
        + _history_col("source_endpoint") + " AS source_endpoint, "
        + _history_col("prev_close_price") + " AS prev_close_price, "
        + _history_col("prev_close_date") + " AS prev_close_date, "
        "c.cisin AS cisin "
        "FROM shareholder_holdings h "
        "LEFT JOIN (SELECT ins_code, date, "
        "CASE WHEN MIN(total_company_shares)=MAX(total_company_shares) "
        "THEN MAX(total_company_shares) ELSE NULL END AS total_company_shares "
        "FROM company_daily GROUP BY ins_code, date) cd "
        "ON cd.ins_code=h.ins_code AND cd.date=h.date "
        "LEFT JOIN companies c ON c.ins_code=h.ins_code "
        "LEFT JOIN (SELECT ins_code, COALESCE(effective_date,date) AS event_date, "
        "GROUP_CONCAT(action_type, '; ') AS action_type, MAX(ratio) AS ratio "
        "FROM corporate_actions GROUP BY ins_code, COALESCE(effective_date,date)) ca "
        "ON ca.ins_code=h.ins_code AND ca.event_date=h.date "
        "WHERE NOT EXISTS ("
        " SELECT 1 FROM sidecar_data.site_excluded_holder_rows x "
        " WHERE x.ins_code=h.ins_code AND x.date=h.date "
        " AND x.id_scheme=h.id_scheme AND x.shareholder_id=h.shareholder_id)")
    history_columns = (
        "id_scheme, shareholder_id, ins_code, symbol, identity_status, date, "
        "jalali_date, shares, ownership_percent_reported, total_company_shares, "
        "price, price_field, price_date, valuation_status, market_value, "
        "previous_date, previous_shares, shares_change, shares_change_percent, "
        "gap_trading_days, change_type, change_calculation_status, corporate_action, "
        "corporate_action_date, corporate_action_ratio, source_change_code, "
        "source_change_amount, anomaly_flags, data_available, note, entry_status, "
        "source_endpoint, prev_close_price, prev_close_date, cisin")
    history_insert = ("INSERT INTO site_holder_history (" + history_columns + ") "
                      "VALUES (" + ",".join("?" for _ in range(35)) + ")")
    def _history_transform(r):
        return (
            r["id_scheme"], r["shareholder_id"], r["ins_code"], r["symbol"],
            r["identity_status"], _norm_date(r["date"]),
            fmt_jalali_date(r["jalali_date"]) if r["jalali_date"] else None,
            r["shares"], r["ownership_percent_reported"], r["total_company_shares"],
            r["price"], r["price_field"],
            _norm_date(r["price_date"]) if r["price_date"] else None,
            r["valuation_status"], r["market_value"],
            _norm_date(r["previous_date"]) if r["previous_date"] else None,
            r["previous_shares"], r["shares_change"], r["shares_change_percent"],
            r["gap_trading_days"], r["change_type"], r["change_calculation_status"],
            r["corporate_action"],
            _norm_date(r["corporate_action_date"]) if r["corporate_action_date"] else None,
            r["corporate_action_ratio"], r["source_change_code"],
            r["source_change_amount"], r["anomaly_flags"],
            int(bool(r["data_available"])), r["note"], r["entry_status"],
            r["source_endpoint"], r["prev_close_price"],
            _norm_date(r["prev_close_date"]) if r["prev_close_date"] else None,
            r["cisin"])
    with sidecar:
        n_hist, dt_hist = _stream_insert(
            cur, sidecar, history_select, history_insert, _history_transform,
            "site_holder_history")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_holder_history_identity_date "
            "ON site_holder_history(id_scheme, shareholder_id, date, ins_code)")
        sidecar.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_holder_history_symbol "
            "ON site_holder_history(id_scheme, shareholder_id, ins_code, date)")
    print("[refresh] site_holder_history: %d observations in %.1fs"
          % (n_hist, dt_hist), flush=True)
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



def _xlsx_column_name(index):
    """Return an Excel column name for a 1-based column index."""
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


SCREENER_PUBLIC_COLUMNS = (
    "symbol", "market", "shareholder_name",
    "ownership_percent_calculated", "shares_change",
    "disclosed_ownership_percent_sum",
)


def _screener_public_row(row):
    """Project a query result onto the fields explicitly approved for clients."""
    return {key: row[key] for key in SCREENER_PUBLIC_COLUMNS}


def _build_screener_xlsx(rows):
    """Build an XLSX export with a strict allow-list of public screener fields."""
    columns = (
        ("symbol", "نماد"),
        ("market", "بازار"),
        ("shareholder_name", "سهام‌دار"),
        ("ownership_percent_calculated", "درصد مالکیت"),
        ("shares_change", "تغییر تعداد سهام"),
        ("disclosed_ownership_percent_sum", "تمرکز مالکیت عمده٪"),
    )

    def cell_xml(cell_ref, value):
        if value is None:
            return '<c r="%s" t="inlineStr"><is><t></t></is></c>' % cell_ref
        if isinstance(value, bool):
            return '<c r="%s" t="b"><v>%d</v></c>' % (cell_ref, int(value))
        if isinstance(value, (int, float, Decimal)):
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                numeric = float("nan")
            if math.isfinite(numeric):
                return '<c r="%s"><v>%s</v></c>' % (cell_ref, str(value))
        text_value = xml_escape(str(value))
        return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t>'
                '</is></c>') % (cell_ref, text_value)

    rows_xml = []
    header = "".join(
        cell_xml("%s1" % _xlsx_column_name(column_index), title)
        for column_index, (_, title) in enumerate(columns, start=1)
    )
    rows_xml.append('<row r="1">%s</row>' % header)

    for row_index, record in enumerate(rows, start=2):
        cells = "".join(
            cell_xml("%s%d" % (_xlsx_column_name(column_index), row_index),
                     record[key])
            for column_index, (key, _) in enumerate(columns, start=1)
        )
        rows_xml.append('<row r="%d">%s</row>' % (row_index, cells))

    last_row = max(1, len(rows) + 1)
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0" rightToLeft="1">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="20"/>'
        '<cols>'
        '<col min="1" max="1" width="16" customWidth="1"/>'
        '<col min="2" max="2" width="18" customWidth="1"/>'
        '<col min="3" max="3" width="30" customWidth="1"/>'
        '<col min="4" max="6" width="22" customWidth="1"/>'
        '</cols>'
        '<sheetData>%s</sheetData>'
        '<autoFilter ref="A1:F%d"/>'
        '</worksheet>'
    ) % ("".join(rows_xml), last_row)

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="نتایج فیلتر" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


class SiteApi:
    """Holds the DB handles and serves every /api/v1/* endpoint."""

    def __init__(self, db_path, sidecar_path, site_dir, output_dir):
        self.db_path = db_path
        self.sidecar_path = sidecar_path
        self.site_dir = site_dir
        self.output_dir = output_dir
        self._refresh_error = None
        self._refresh_lock = threading.Lock()
        self.market_service = MarketService(
            sidecar=self.sidecar,
            live=self.live,
            require_ready=self.require_ready,
            meta=self.meta,
            q_str=_q_str,
            q_int=_q_int,
            like_or_clause=_like_or_clause,
            normalize_fa=normalize_fa,
            norm_date=_norm_date,
            gregorian_yyyymmdd_to_jalali=gregorian_yyyymmdd_to_jalali,
            fmt_gregorian_date=fmt_gregorian_date,
        )
        self.ownership_service = OwnershipService(
            sidecar=self.sidecar,
            live=self.live,
            sidecar_path=self.sidecar_path,
            require_ready=self.require_ready,
            resolve_symbol=self.market_service.resolve_symbol,
            company_daily=self.market_service._company_daily,
            majority_twin_map=self._majority_twin_map,
            q_str=_q_str,
            q_int=_q_int,
            like_or_clause=_like_or_clause,
            jalali_to_gregorian_date=jalali_to_gregorian_date,
            gregorian_yyyymmdd_to_jalali=gregorian_yyyymmdd_to_jalali,
            fmt_gregorian_date=fmt_gregorian_date,
            fmt_jalali_date=fmt_jalali_date,
            norm_date=_norm_date,
            latest_close=_latest_close,
        )
        self.portfolio_service = PortfolioService(
            sidecar=self.sidecar,
            live=self.live,
            site_dir=self.site_dir,
            require_ready=self.require_ready,
            majority_twin_map=self._majority_twin_map,
            like_or_clause=_like_or_clause,
            q_str=_q_str,
            q_int=_q_int,
            norm_date=_norm_date,
            fmt_jalali_date=fmt_jalali_date,
            gregorian_yyyymmdd_to_jalali=gregorian_yyyymmdd_to_jalali,
            ownership_percent_for_date=ownership_percent_for_date,
            classify_portfolio_change=classify_portfolio_change,
            classify_historical_exit=classify_historical_exit,
            build_portfolio_history_summary=build_portfolio_history_summary,
            load_sector_reference=load_sector_reference,
            sector_affinity_accumulator_factory=SectorAffinityAccumulator,
        )

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
        """Refresh derived projections with thread and process-level exclusion."""
        with self._refresh_lock:
            try:
                with ProcessLock(self.sidecar_path + ".refresh.lock"):
                    self._refresh_locked()
            except LockAlreadyHeldError as exc:
                self._refresh_error = str(exc)
                print("[refresh] skipped: %s" % exc, flush=True)

    def _refresh_locked(self):
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
                history_ready = sc.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='site_holder_history'").fetchone() is not None
                if have == fingerprint and n_watch > 0 and history_ready and \
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
        # and legacy sidecars that predate the historical-holder ledger.
        row = sc.execute(
            "SELECT value FROM site_meta WHERE key='last_refresh'").fetchone()
        ownership_schema = self.meta(sc, "ownership_resolver_schema")
        sidecar_version = self.meta(sc, "sidecar_version")
        history_table = sc.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='site_holder_history'").fetchone()
        if (not row or not row[0]
                or ownership_schema != "resolved-v1-source-exclusions"
                or sidecar_version != str(SIDECAR_VERSION)
                or not history_table):
            raise ApiError(503, "داده‌ها هنوز آماده نیستند؛ ساخت کامل Sidecar لازم است."
                            + (" خطا: " + self._refresh_error
                               if self._refresh_error else ""))

    def resolve_symbol(self, sc, symbol):
        row = self._resolve_symbol_row(sc, symbol)
        if row is None:
            raise ApiError(404, "نماد یافت نشد: %s" % symbol)
        return row

    # -- Market Service compatibility adapters -----------------------------
    def market_overview(self):
        return self.market_service.market_overview()

    def market_watch(self, params):
        return self.market_service.market_watch(params)

    def stocks_search(self, params):
        return self.market_service.stocks_search(params)

    def _resolve_symbol_row(self, sc, symbol):
        return self.market_service._resolve_symbol_row(sc, symbol)

    def _company_daily(self, live, ins_code):
        return self.market_service._company_daily(live, ins_code)

    def stock_detail(self, symbol):
        return self.market_service.stock_detail(symbol)

    # -- Ownership Service compatibility adapters ---------------------------
    def stock_shareholders(self, symbol, params):
        return self.ownership_service.stock_shareholders(symbol, params)

    def _resolve_date_rows(self, live, ins_code, date_g, rows):
        return resolve_date_rows(live, ins_code, date_g, rows)

    def stock_ownership_series(self, symbol, params):
        return self.ownership_service.stock_ownership_series(symbol, params)
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
        return self.market_service.stock_prices(symbol)
    # -- 4. Cross-company shareholder search -------------------------------
    def _search_name_pairs(self, live, q):
        return self.ownership_service._search_name_pairs(live, q)

    def _search_holder_rows(self, live, q, symbol, stype, min_pct, multi, limit, offset):
        return self.ownership_service._search_holder_rows(
            live, q, symbol, stype, min_pct, multi, limit, offset)

    def shareholders_search(self, params):
        return self.ownership_service.shareholders_search(params)

    def shareholder_observation_history(self, params):
        return self.ownership_service.shareholder_observation_history(params)
    # -- 5. Investor portfolios ---------------------------------------------
    # -- Portfolio Service compatibility adapters ---------------------------
    def portfolio_investors(self, params):
        return self.portfolio_service.portfolio_investors(params)

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
        return self.portfolio_service._linked_identity_ids(live, sc, scheme, sh_id)

    def _holder_positions(self, live, sc, ids, as_of_dashed):
        return self.portfolio_service._holder_positions(live, sc, ids, as_of_dashed)

    def _holder_history_stream(self, live, ids, as_of_dashed=None, ins_code=None):
        return self.portfolio_service._holder_history_stream(
            live, ids, as_of_dashed=as_of_dashed, ins_code=ins_code)

    def _iter_portfolio_history_observations(
            self, live, ids, requested_pair, as_of_dashed=None, ins_code=None):
        return self.portfolio_service._iter_portfolio_history_observations(
            live, ids, requested_pair, as_of_dashed=as_of_dashed, ins_code=ins_code)

    def portfolio_history(self, investor_id, params):
        return self.portfolio_service.portfolio_history(investor_id, params)

    def portfolio_detail(self, investor_id, params):
        return self.portfolio_service.portfolio_detail(investor_id, params)

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
            return {"rows": [_screener_public_row(r) for r in rows]}
        finally:
            sc.close()

    def screener_export(self, body):
        """Export the selected filter AST as XLSX, capped and column-allow-listed."""
        limit = 5000
        sql, args, _ = self._screener_query(body, limit)
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            rows = sc.execute(sql, args + [limit]).fetchall()
        finally:
            sc.close()
        safe_rows = [_screener_public_row(row) for row in rows]
        return _build_screener_xlsx(safe_rows)

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

    def _send_attachment(self, status, body, content_type, filename):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
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
            index_target = os.path.join(target, "index.html")
            legacy_target = os.path.join(target, "ai_studio_code.html")
            target = index_target if os.path.isfile(index_target) else legacy_target
        if not os.path.isfile(target):
            self._send_json(404, {"detail": "فایل یافت نشد."})
            return
        extension = os.path.splitext(target)[1].lower()
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".htm": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".webp": "image/webp",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        ctype = content_types.get(extension, "application/octet-stream")
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
            if path in ("/", "/index.html"):
                pages_entry = os.path.join(self.api.site_dir, "index.html")
                entry = "index.html" if os.path.isfile(pages_entry) else "ai_studio_code.html"
                self._serve_file(entry)
                status = 200
                return
            if path == "/ai_studio_code.html":
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
            if ctype == "xlsx":
                self._send_attachment(
                    status, payload,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "tloomberg_shareholder_screener.xlsx")
                print("%s %s -> %d xlsx %.2fs" % (
                    method, path, status, time.time() - t0), flush=True)
                return
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
        """Compatibility adapter; routing lives in site_api.dispatch."""
        return dispatch_api(self.api, method, path, params, body)
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
    ap.add_argument("--site-dir", default=os.path.join(here, "docs"),
                    help="folder holding the local website entrypoint (docs/ by default; pass . for the legacy ai_studio_code.html)")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT,
                    help="folder holding the Excel workbooks")
    ap.add_argument("--sidecar", default=os.path.join(here, "site_data.sqlite"),
                    help="path to the derived site SQLite database")
    ap.add_argument("--refresh-mode", choices=("inline", "external"),
                    default="inline",
                    help="inline keeps legacy in-process refresh; external uses site_api.refresh_worker")
    ap.add_argument("--refresh-minutes", type=float, default=10,
                    help="refresh interval in minutes (0 = one pass)")
    args = ap.parse_args()
    if not math.isfinite(args.refresh_minutes) or args.refresh_minutes < 0:
        ap.error("--refresh-minutes must be a finite non-negative number")

    if not os.path.isfile(args.db):
        print("ERROR: database not found: %s" % args.db)
        raise SystemExit(2)
    if not any(os.path.isfile(os.path.join(args.site_dir, name))
               for name in ("index.html", "ai_studio_code.html")):
        print("ERROR: index.html or ai_studio_code.html not found in %s" % args.site_dir)
        raise SystemExit(2)

    # Keep the generated API sidecar beside server.py, not inside docs/,
    # because docs/ is the public Pages directory and may be committed.
    api = SiteApi(args.db, args.sidecar, args.site_dir, args.output_dir)
    Handler.api = api

    print("tsetmc_site API server")
    print("  db:     %s (read-only)" % args.db)
    print("  site:   %s" % args.site_dir)
    print("  output: %s" % args.output_dir)
    print("  sidecar:%s" % args.sidecar)
    try:
        live = api.live()
        try:
            _verify_jalali(live)
        finally:
            live.close()
    except Exception as e:
        print("WARNING: could not open the live DB: %r" % (e,))

    _refresh_threads = start_refresh_runtime(
        api, args.refresh_mode, args.refresh_minutes
    )
    if args.refresh_mode == "inline":
        print("building summaries with the legacy in-process refresh mode ...",
              flush=True)
    else:
        worker_command = (
            "python -m site_api.refresh_worker --db %s --sidecar %s "
            "--refresh-minutes %s"
            % (repr(args.db), repr(args.sidecar), args.refresh_minutes)
        )
        print("[refresh] external worker mode selected. Start a separate "
              "terminal and run:\n  " + worker_command, flush=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("listening on http://%s:%d/ -- open it in the browser" % (args.host, args.port),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")


if __name__ == "__main__":
    main()