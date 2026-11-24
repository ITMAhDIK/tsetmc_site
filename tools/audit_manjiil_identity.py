#!/usr/bin/env python3
"""Read-only identity audit for the Manjil workbook.

Prints only records matching the requested legal-entity name and the source
identity/date/quantity fields needed to decide whether ondate_share_id values
refer to one shareholder across snapshots. No workbook is modified.
"""
from __future__ import annotations
import json
import re
import unicodedata
from pathlib import Path
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_DIR = ROOT / "docs/data/workbooks"
NEEDLE = "شرکت مدیریت انرژی امیدتابان هور"


def norm(value):
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = s.replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه")
    s = re.sub(r"[\u200c\s]+", " ", s).strip()
    s = re.sub(r"[‐‑‒–—-]+", "-", s)
    return s


def rows(sheet):
    iterator = sheet.iter_rows(values_only=True)
    headers = next(iterator, ())
    names = [str(x).strip() if x is not None else "" for x in headers]
    for values in iterator:
        if not values or not any(x is not None for x in values):
            continue
        yield {name: values[i] for i, name in enumerate(names) if name and i < len(values)}


def main():
    matches = list(WORKBOOK_DIR.glob("*19239905145688600.xlsx"))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one Manjil workbook ending 19239905145688600.xlsx; found {len(matches)}")
    path = matches[0]
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        history = list(rows(wb["holdings_history"]))
        shareholders = list(rows(wb["shareholders"]))
        target_history = [
            r for r in history
            if NEEDLE in norm(r.get("shareholder_name"))
        ]
        target_shareholders = [
            r for r in shareholders
            if NEEDLE in norm(r.get("shareholder_name"))
        ]

        ids = {}
        for r in target_history:
            key = (str(r.get("id_scheme") or ""), str(r.get("shareholder_id") or ""))
            entry = ids.setdefault(key, {
                "id_scheme": key[0],
                "shareholder_id": key[1],
                "raw_id_scheme": str(r.get("raw_id_scheme") or ""),
                "raw_source_id": str(r.get("raw_source_id") or ""),
                "rows": 0,
                "dates": [],
                "shares_values": [],
                "names": set(),
                "identity_statuses": set(),
                "types": set(),
            })
            entry["rows"] += 1
            entry["dates"].append(str(r.get("date_gregorian") or r.get("date") or ""))
            entry["shares_values"].append(r.get("shares"))
            entry["names"].add(norm(r.get("shareholder_name")))
            entry["identity_statuses"].add(str(r.get("identity_status") or ""))
            entry["types"].add(str(r.get("shareholder_type") or ""))

        output_ids = []
        for entry in ids.values():
            dates = sorted(set(x for x in entry.pop("dates") if x))
            shares = [x for x in entry.pop("shares_values") if x is not None]
            output_ids.append({
                **entry,
                "dates": dates,
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "distinct_dates": len(dates),
                "min_shares": min(shares) if shares else None,
                "max_shares": max(shares) if shares else None,
                "names": sorted(entry["names"]),
                "identity_statuses": sorted(entry["identity_statuses"]),
                "types": sorted(entry["types"]),
            })
        output = {
            "workbook": path.name,
            "target_name": NEEDLE,
            "history_rows_matched": len(target_history),
            "shareholders_rows_matched": len(target_shareholders),
            "distinct_identity_pairs": len(output_ids),
            "id_scheme_counts": {},
            "identities": sorted(output_ids, key=lambda x: (x["first_date"] or "", x["shareholder_id"])),
            "shareholders_sheet": [
                {
                    "shareholder_id": str(r.get("shareholder_id") or ""),
                    "id_scheme": str(r.get("id_scheme") or ""),
                    "raw_source_id": str(r.get("raw_source_id") or ""),
                    "identity_verification_status": str(r.get("identity_verification_status") or ""),
                    "first_observed_date_gregorian": str(r.get("first_observed_date_gregorian") or ""),
                    "last_observed_date_gregorian": str(r.get("last_observed_date_gregorian") or ""),
                    "shareholder_name": str(r.get("shareholder_name") or ""),
                }
                for r in target_shareholders
            ],
        }
        for item in output_ids:
            output["id_scheme_counts"][item["id_scheme"]] = output["id_scheme_counts"].get(item["id_scheme"], 0) + 1
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    finally:
        wb.close()


if __name__ == "__main__":
    main()
