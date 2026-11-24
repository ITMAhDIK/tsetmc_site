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
        rows_by_pair = {}
        for r in target_history:
            key = (str(r.get("id_scheme") or ""), str(r.get("shareholder_id") or ""))
            date_key = str(r.get("date_gregorian") or r.get("date") or "")
            name_key = norm(r.get("shareholder_name"))
            type_key = str(r.get("shareholder_type") or "")
            shares = r.get("shares")
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
            entry["dates"].append(date_key)
            entry["shares_values"].append(shares)
            entry["names"].add(name_key)
            entry["identity_statuses"].add(str(r.get("identity_status") or ""))
            entry["types"].add(type_key)
            rows_by_pair.setdefault(key, {})[date_key] = {
                "name": name_key, "type": type_key, "shares": shares
            }

        # Build only exact cross-ID links: same date, same normalized legal name,
        # same shareholder type, same share quantity, and exactly two IDs on
        # that signature. Any ambiguous/mismatching overlap is not auto-linked.
        signature_pairs = {}
        for pair, dated in rows_by_pair.items():
            for date_key, row in dated.items():
                if not date_key or row["shares"] is None:
                    continue
                numeric_shares = float(row["shares"])
                shares_key = str(int(numeric_shares)) if numeric_shares.is_integer() else format(numeric_shares, ".12g")
                signature = (date_key, row["name"], row["type"], shares_key)
                signature_pairs.setdefault(signature, set()).add(pair)

        parent = {pair: pair for pair in rows_by_pair}
        def find(pair):
            while parent[pair] != pair:
                parent[pair] = parent[parent[pair]]
                pair = parent[pair]
            return pair
        exact_edges = []
        ambiguous_signatures = []
        for signature, pairs in signature_pairs.items():
            if len(pairs) == 2:
                a, b = tuple(pairs)
                ra, rb = rows_by_pair[a][signature[0]], rows_by_pair[b][signature[0]]
                # Require no contradictory quantity on any shared date.
                shared_dates = set(rows_by_pair[a]) & set(rows_by_pair[b])
                conflict = any(
                    rows_by_pair[a][d]["shares"] != rows_by_pair[b][d]["shares"]
                    for d in shared_dates
                )
                if not conflict:
                    root_a, root_b = find(a), find(b)
                    if root_a != root_b:
                        parent[root_b] = root_a
                    exact_edges.append({
                        "date": signature[0],
                        "shares": signature[3],
                        "left": f"{a[0]}:{a[1]}",
                        "right": f"{b[0]}:{b[1]}",
                        "shared_dates": sorted(shared_dates),
                    })
            elif len(pairs) > 2:
                ambiguous_signatures.append({
                    "date": signature[0], "shares": signature[3],
                    "identity_pairs": sorted(f"{p[0]}:{p[1]}" for p in pairs)
                })

        components = {}
        for pair in rows_by_pair:
            components.setdefault(find(pair), []).append(pair)
        component_list = [
            sorted(f"{p[0]}:{p[1]}" for p in members)
            for members in components.values()
        ]

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
            "matched_records": [
                {
                    "id_scheme": str(r.get("id_scheme") or ""),
                    "shareholder_id": str(r.get("shareholder_id") or ""),
                    "date": str(r.get("date_gregorian") or r.get("date") or ""),
                    "shares": r.get("shares"),
                    "name": str(r.get("shareholder_name") or ""),
                    "type": str(r.get("shareholder_type") or ""),
                }
                for r in target_history
            ],
            "target_name": NEEDLE,
            "history_rows_matched": len(target_history),
            "shareholders_rows_matched": len(target_shareholders),
            "distinct_identity_pairs": len(output_ids),
            "exact_overlap_edges": exact_edges,
            "exact_overlap_edge_count": len(exact_edges),
            "ambiguous_same_signature_count": len(ambiguous_signatures),
            "ambiguous_same_signatures": ambiguous_signatures,
            "identity_components_after_exact_overlap_only": component_list,
            "component_count_after_exact_overlap_only": len(component_list),
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
