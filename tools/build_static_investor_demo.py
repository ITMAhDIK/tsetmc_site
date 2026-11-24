#!/usr/bin/env python3
"""Build a static, read-only investor demo from the 14 uploaded workbooks.

Identity is grouped only by the source namespace pair (id_scheme, shareholder_id).
Names are display metadata only and are never used to merge investors.
The sector affinity calculations reuse the repository's SectorAffinityAccumulator.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_DIR = ROOT / "docs" / "data" / "workbooks"
DATA_DIR = ROOT / "docs" / "data"
OUTPUT = DATA_DIR / "investor_demo.json"

sys.path.insert(0, str(ROOT))
from sector_affinity import SectorAffinityAccumulator, load_sector_reference  # noqa: E402


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split()).strip()
    return value or None


def id_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    raw = str(value).strip()
    return raw or None


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value).strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def read_sheet(path: Path, sheet_name: str) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            return []
        ws = wb[sheet_name]
        iterator = ws.iter_rows(values_only=True)
        header_row = next(iterator, None)
        if not header_row:
            return []
        headers = [text_value(item) for item in header_row]
        output = []
        for values in iterator:
            if not values or not any(value is not None for value in values):
                continue
            row = {}
            for index, key in enumerate(headers):
                if key:
                    row[key] = values[index] if index < len(values) else None
            output.append(row)
        return output
    finally:
        wb.close()


def workbook_identity(path: Path) -> tuple[str, str]:
    # The exchange instrument code is the numeric suffix in each supplied filename.
    match = re.search(r"_(\d+)$", path.stem)
    if not match:
        raise ValueError(f"Workbook filename has no instrument code suffix: {path.name}")
    return path.stem[:match.start()], match.group(1)


def to_json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def main() -> int:
    files = sorted(WORKBOOK_DIR.glob("*.xlsx"))
    if not files:
        raise SystemExit(f"No workbooks found in {WORKBOOK_DIR}")

    registry, categories, registry_meta = load_sector_reference(ROOT / "docs")
    investors: dict[str, dict[str, Any]] = {}
    symbol_latest_date: dict[str, str] = {}
    symbol_latest_label: dict[str, str] = {}
    diagnostics = {
        "shareholder_types": Counter(),
        "identity_statuses": Counter(),
        "valuation_statuses": Counter(),
        "price_fields": Counter(),
        "change_statuses": Counter(),
        "id_schemes": Counter(),
    }
    symbol_summary = []
    total_history_rows = 0
    valid_history_rows = 0
    skipped_no_identity = 0
    all_dates = []

    for path in files:
        symbol_from_name, ins_code = workbook_identity(path)
        company_rows = read_sheet(path, "company_history")
        shareholder_rows = read_sheet(path, "shareholders")
        history_rows = read_sheet(path, "holdings_history")

        for company_row in company_rows:
            candidate = iso_date(company_row.get("date_gregorian")) or iso_date(company_row.get("date"))
            if candidate and (ins_code not in symbol_latest_date or candidate > symbol_latest_date[ins_code]):
                symbol_latest_date[ins_code] = candidate
            symbol_latest_label[ins_code] = text_value(company_row.get("symbol")) or symbol_from_name

        metadata_by_pair = {}
        for holder in shareholder_rows:
            scheme = text_value(holder.get("id_scheme"))
            holder_id = id_value(holder.get("shareholder_id"))
            if not scheme or not holder_id:
                continue
            pair_key = f"{scheme}:{holder_id}"
            metadata_by_pair[pair_key] = {
                "shareholder_name": text_value(holder.get("shareholder_name")),
                "shareholder_type": text_value(holder.get("shareholder_type")),
                "identity_verification_status": text_value(holder.get("identity_verification_status")),
            }

        total_history_rows += len(history_rows)
        symbol = symbol_from_name
        for source_row in history_rows:
            scheme = text_value(source_row.get("id_scheme"))
            holder_id = id_value(source_row.get("shareholder_id"))
            if not scheme or not holder_id:
                skipped_no_identity += 1
                continue

            pair_key = f"{scheme}:{holder_id}"
            observed_date = (
                iso_date(source_row.get("date_gregorian"))
                or iso_date(source_row.get("date"))
            )
            if not observed_date:
                continue

            valid_history_rows += 1
            all_dates.append(observed_date)
            diagnostics["id_schemes"][scheme] += 1
            diagnostics["shareholder_types"][text_value(source_row.get("shareholder_type")) or "unknown"] += 1
            diagnostics["identity_statuses"][text_value(source_row.get("identity_status")) or "unknown"] += 1
            diagnostics["valuation_statuses"][text_value(source_row.get("valuation_status")) or "unknown"] += 1
            diagnostics["price_fields"][text_value(source_row.get("price_field")) or "unknown"] += 1
            diagnostics["change_statuses"][text_value(source_row.get("change_calculation_status")) or "unknown"] += 1

            investor = investors.setdefault(pair_key, {
                "id": pair_key,
                "id_scheme": scheme,
                "shareholder_id": holder_id,
                "shareholder_name": None,
                "shareholder_type": None,
                "identity_verification_status": None,
                "symbols": {},
                "timeline": [],
            })
            holder_meta = metadata_by_pair.get(pair_key, {})
            candidate_name = (
                text_value(source_row.get("shareholder_name"))
                or holder_meta.get("shareholder_name")
            )
            if candidate_name and not investor["shareholder_name"]:
                investor["shareholder_name"] = candidate_name
            elif candidate_name and investor["shareholder_name"] != candidate_name:
                investor.setdefault("name_variants", set()).add(candidate_name)
            candidate_type = (
                text_value(source_row.get("shareholder_type"))
                or holder_meta.get("shareholder_type")
            )
            if candidate_type and not investor["shareholder_type"]:
                investor["shareholder_type"] = candidate_type
            if holder_meta.get("identity_verification_status"):
                investor["identity_verification_status"] = holder_meta["identity_verification_status"]

            row = {
                "symbol": text_value(source_row.get("symbol")) or symbol,
                "ins_code": ins_code,
                "date": observed_date,
                "shares": number(source_row.get("shares")),
                "price": number(source_row.get("price")),
                "market_value_rial": number(source_row.get("market_value")),
                "ownership_pct": (
                    number(source_row.get("ownership_percent_calculated"))
                    if number(source_row.get("ownership_percent_calculated")) is not None
                    else number(source_row.get("ownership_percent_reported"))
                ),
                "shares_change": number(source_row.get("shares_change")),
                "shares_change_percent": number(source_row.get("shares_change_percent")),
                "change_type": text_value(source_row.get("change_type")),
                "change_calculation_status": text_value(source_row.get("change_calculation_status")),
                "corporate_action": text_value(source_row.get("corporate_action")),
                "corporate_action_date": iso_date(source_row.get("corporate_action_date")),
                "price_date": iso_date(source_row.get("price_date")) or observed_date,
                "price_field": text_value(source_row.get("price_field")),
                "valuation_status": text_value(source_row.get("valuation_status")),
                "identity_status": text_value(source_row.get("identity_status")),
                "anomaly_flags": text_value(source_row.get("anomaly_flags")),
                "source": text_value(source_row.get("source")),
            }
            investor["timeline"].append(row)
            investor["symbols"].setdefault(ins_code, {
                "symbol": row["symbol"],
                "ins_code": ins_code,
                "observations": [],
            })["observations"].append(row)

    # Finalize each investor using only its strict (scheme, ID) namespace pair.
    final_investors = []
    aggregate_stats = {
        "current_positions": 0,
        "timeline_rows": 0,
        "investors_with_affinity": 0,
        "mapped_positions": 0,
        "unmapped_positions": 0,
    }

    for investor in investors.values():
        timeline = sorted(investor["timeline"], key=lambda r: (r["date"], r["ins_code"]))
        investor["timeline"] = timeline

        symbol_items = []
        current_value_rial = 0.0
        current_value_known = False
        for ins_code, symbol_info in investor["symbols"].items():
            records = sorted(symbol_info["observations"], key=lambda r: r["date"])
            if not records:
                continue
            first = records[0]
            last = records[-1]
            company_last_date = symbol_latest_date.get(ins_code)
            has_later_snapshot = bool(company_last_date and company_last_date > last["date"])
            exit_status = "assumed_sold" if has_later_snapshot else "currently_disclosed"
            if has_later_snapshot:
                # This describes absence from later disclosed snapshots, not a proven sale.
                pass
            latest_value = last.get("market_value_rial")
            latest_price = last.get("price")
            if latest_value is None and last.get("shares") is not None and latest_price is not None:
                latest_value = last["shares"] * latest_price
            if exit_status == "currently_disclosed" and latest_value is not None:
                current_value_rial += latest_value
                current_value_known = True
                aggregate_stats["current_positions"] += 1

            symbol_items.append({
                "symbol": last["symbol"],
                "ins_code": ins_code,
                "first_observation_date": first["date"],
                "last_observation_date": last["date"],
                "company_latest_date": company_last_date,
                "last_observation_shares": last.get("shares"),
                "last_observation_price": latest_price,
                "last_observation_market_value_rial": latest_value,
                "last_observation_ownership_pct": last.get("ownership_pct"),
                "last_observation_shares_change": last.get("shares_change"),
                "observations_count": len(records),
                "exit_status": exit_status,
            })

        # Reuse the project's production affinity scoring engine, with the selected
        # composite ID explicitly supplied so exact rows are not reclassified by name.
        accumulator = SectorAffinityAccumulator(
            registry=registry,
            categories=categories,
            requested_pair=(investor["id_scheme"], investor["shareholder_id"]),
            half_life_months=6.0,
            rotation_lookback_days=90,
        )
        engine_rows = []
        for row in timeline:
            status_text = (row.get("identity_status") or "").lower()
            identity_conflict = "conflict" in status_text
            engine_rows.append({
                "id_scheme": investor["id_scheme"],
                "shareholder_id": investor["shareholder_id"],
                "date": row["date"],
                "ins_code": row["ins_code"],
                "shares": row.get("shares"),
                "reference_price": row.get("price"),
                "reference_price_date": row.get("price_date") or row["date"],
                "shares_change": row.get("shares_change"),
                "change_calculation_status": row.get("change_calculation_status"),
                "corporate_action": row.get("corporate_action"),
                "anomaly_flags": row.get("anomaly_flags"),
                "identity_status": row.get("identity_status"),
                "identity_conflict": identity_conflict,
            })
        for engine_row in engine_rows:
            accumulator.add(engine_row)
        affinity = accumulator.result()
        if affinity.get("dashboard_categories") or affinity.get("official_groups"):
            aggregate_stats["investors_with_affinity"] += 1

        # Keep all source observations for the per-ID history table, but strip
        # low-level fetch URLs and raw identifiers from the public demo payload.
        clean_timeline = []
        for row in timeline:
            clean_timeline.append({
                "symbol": row["symbol"],
                "ins_code": row["ins_code"],
                "date": row["date"],
                "shares": row.get("shares"),
                "price_rial": row.get("price"),
                "market_value_rial": row.get("market_value_rial"),
                "ownership_pct": row.get("ownership_pct"),
                "shares_change": row.get("shares_change"),
                "shares_change_pct": row.get("shares_change_percent"),
                "change_type": row.get("change_type"),
                "change_calculation_status": row.get("change_calculation_status"),
                "corporate_action": row.get("corporate_action"),
                "corporate_action_date": row.get("corporate_action_date"),
                "price_date": row.get("price_date"),
                "valuation_status": row.get("valuation_status"),
                "identity_status": row.get("identity_status"),
            })

        symbol_items.sort(key=lambda x: (x["symbol"] or "", x["ins_code"]))
        latest_change = next(
            (row.get("shares_change") for row in reversed(timeline) if row.get("shares_change") is not None),
            None,
        )
        max_ownership = max(
            (row["ownership_pct"] for row in timeline if row.get("ownership_pct") is not None),
            default=None,
        )
        final_investors.append({
            "id": investor["id"],
            "id_scheme": investor["id_scheme"],
            "shareholder_id": investor["shareholder_id"],
            "shareholder_name": investor.get("shareholder_name") or "نام افشا نشده",
            "name_variants": sorted(investor.get("name_variants", set())),
            "shareholder_type": investor.get("shareholder_type") or "unknown",
            "identity_verification_status": investor.get("identity_verification_status"),
            "n_companies": len(symbol_items),
            "latest_observation_date": max((r["date"] for r in timeline), default=None),
            "current_portfolio_value_rial": round(current_value_rial, 2) if current_value_known else None,
            "max_ownership_pct": max_ownership,
            "latest_shares_change": latest_change,
            "symbols": symbol_items,
            "timeline": clean_timeline,
            "sector_affinity": affinity,
        })
        aggregate_stats["timeline_rows"] += len(clean_timeline)
        aggregate_stats["mapped_positions"] += sum(
            1 for item in symbol_items if item["ins_code"] in registry
        )
        aggregate_stats["unmapped_positions"] += sum(
            1 for item in symbol_items if item["ins_code"] not in registry
        )

    final_investors.sort(key=lambda item: (
        item["n_companies"], item["current_portfolio_value_rial"] or 0
    ), reverse=True)

    if not all_dates:
        raise SystemExit("No usable Gregorian observation dates found in workbook histories.")

    summary = {
        "workbook_count": len(files),
        "symbol_count": len({workbook_identity(path)[1] for path in files}),
        "history_rows_in_source": total_history_rows,
        "history_rows_with_composite_id_and_date": valid_history_rows,
        "history_rows_without_composite_id": skipped_no_identity,
        "investor_count": len(final_investors),
        "earliest_observation_date": min(all_dates),
        "latest_observation_date": max(all_dates),
        "sector_registry": registry_meta,
        "aggregate": aggregate_stats,
        "diagnostics": {key: dict(counter) for key, counter in diagnostics.items()},
    }
    document = {
        "schema_version": 1,
        "generated_from": "14 uploaded per-symbol Excel workbooks",
        "scope_note": (
            "This is a sample derived only from the 14 supplied workbooks. "
            "Identity is grouped by exact (id_scheme, shareholder_id); names are never merge keys. "
            "An assumed exit means absence from a later disclosed snapshot, not proof of a sale."
        ),
        "methodology": {
            "identity_key": "id_scheme + ':' + shareholder_id",
            "name_matching": False,
            "affinity_engine": "SectorAffinityAccumulator from sector_affinity.py",
            "affinity_weights": {
                "current_allocation": 0.50,
                "historical_affinity": 0.30,
                "recent_net_rotation": 0.20,
            },
            "historical_half_life_months": 6,
            "rotation_lookback_days": 90,
            "rotation_rule": "consecutive shares change x observation-date reference price; excludes gaps/corporate actions/anomalies per engine",
        },
        "summary": summary,
        "investors": final_investors,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), default=to_json_safe, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )

    # Logs contain aggregate metadata only; no IDs, names, or row-level examples.
    print(json.dumps({"static_investor_demo": summary}, ensure_ascii=False, indent=2))
    print(f"STATIC_DEMO_OUTPUT={OUTPUT.relative_to(ROOT).as_posix()}")
    print(f"STATIC_DEMO_BYTES={OUTPUT.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
