#!/usr/bin/env python3
"""Fetch official TSETMC group/subgroup codes for local instruments.

The source SQLite database is read-only. Output is atomic and resumable.
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://cdn.tsetmc.com/api/Instrument/GetInstrumentIdentity"


def _as_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _find_instrument_object(value):
    candidates = []

    def walk(node):
        if isinstance(node, dict):
            if "cSecVal" in node or "lSecVal" in node:
                candidates.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    if not candidates:
        return value if isinstance(value, dict) else {}
    return max(candidates, key=lambda item: (
        int("cSecVal" in item) + int("lSecVal" in item),
        int("subSector" in item),
    ))


def _find_subsector(instrument, payload):
    value = instrument.get("subSector")
    if value is None and isinstance(payload, dict):
        value = payload.get("subSector")
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else {}


def extract_sector_fields(payload, ins_code):
    instrument = _find_instrument_object(payload)
    subsector = _find_subsector(instrument, payload)
    return {
        "ins_code": str(ins_code),
        "cSecVal": _as_text(instrument.get("cSecVal")),
        "lSecVal": _as_text(instrument.get("lSecVal")),
        "cSoSecVal": _as_text(
            subsector.get("cSoSecVal") or instrument.get("cSoSecVal")
        ),
        "lSoSecVal": _as_text(
            subsector.get("lSoSecVal") or instrument.get("lSoSecVal")
        ),
    }


def load_instruments(db_path):
    absolute = os.path.abspath(db_path)
    uri = "file:" + urllib.request.pathname2url(absolute) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(companies)"
        ).fetchall()}
        if "ins_code" not in columns:
            raise RuntimeError("companies table has no ins_code column")
        symbol_expr = "symbol" if "symbol" in columns else "NULL"
        rows = connection.execute(
            "SELECT ins_code, %s FROM companies "
            "WHERE ins_code IS NOT NULL AND TRIM(CAST(ins_code AS TEXT))<>'' "
            "ORDER BY ins_code" % symbol_expr
        ).fetchall()
        unique = {}
        for ins_code, symbol in rows:
            unique[str(ins_code)] = {
                "ins_code": str(ins_code),
                "symbol": str(symbol) if symbol is not None else None,
            }
        return list(unique.values())
    finally:
        connection.close()


def load_existing(path):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    instruments = document.get("instruments", {})
    return instruments if isinstance(instruments, dict) else {}


def save_atomic(path, instruments):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    document = {
        "schema_version": 1,
        "source": DEFAULT_BASE_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instruments": instruments,
    }
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def fetch_one(ins_code, base_url, timeout, retries):
    url = base_url.rstrip("/") + "/" + urllib.parse.quote(str(ins_code), safe="")
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "tsetmc-site-sector-registry/1.0",
        "Referer": "https://www.tsetmc.com/",
    })
    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            fields = extract_sector_fields(payload, ins_code)
            if not fields["cSecVal"] and not fields["lSecVal"]:
                raise ValueError("response has no cSecVal/lSecVal fields")
            return fields
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                json.JSONDecodeError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(str(last_error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to tsetmc_holdings SQLite database")
    parser.add_argument("--output", default="data/instrument_sectors.json")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--delay", type=float, default=0.12)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0, help="Maximum instruments; 0 means all")
    parser.add_argument("--force", action="store_true", help="Re-fetch records already present")
    parser.add_argument("--save-every", type=int, default=25)
    args = parser.parse_args()

    if args.timeout <= 0 or args.delay < 0 or args.retries < 0 or args.save_every < 1:
        parser.error("timeout/save-every must be positive; delay/retries cannot be negative")

    instruments = load_instruments(args.db)
    existing = load_existing(args.output)
    if args.limit:
        instruments = instruments[:args.limit]
    pending = [
        item for item in instruments
        if args.force or item["ins_code"] not in existing
        or not existing[item["ins_code"]].get("cSecVal")
    ]
    print("Instruments: %d | already present: %d | to fetch: %d" % (
        len(instruments), len(instruments) - len(pending), len(pending)
    ), flush=True)

    success = failures = 0
    for index, item in enumerate(pending, 1):
        ins_code = item["ins_code"]
        try:
            fields = fetch_one(ins_code, args.base_url, args.timeout, args.retries)
            fields["symbol"] = item.get("symbol")
            existing[ins_code] = fields
            success += 1
        except Exception as error:
            failures += 1
            print("[failed] %s (%s): %s" % (
                ins_code, item.get("symbol") or "?", error
            ), flush=True)
        if index % args.save_every == 0:
            save_atomic(args.output, existing)
            print("[progress] %d/%d | success=%d | failed=%d" % (
                index, len(pending), success, failures
            ), flush=True)
        if args.delay:
            time.sleep(args.delay)

    save_atomic(args.output, existing)
    print("Completed. fetched=%d failed=%d stored=%d output=%s" % (
        success, failures, len(existing), args.output
    ), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
