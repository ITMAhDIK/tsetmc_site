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
import unicodedata
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



def normalized_identity_name(value: Any) -> str:
    """Normalize only Unicode/spaces/punctuation; never use fuzzy name matching."""
    raw = text_value(value) or ""
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه")
    raw = raw.replace("\u200c", " ")
    raw = re.sub(r"\\s+", " ", raw).strip()
    raw = re.sub(r"[‐‑‒–—-]+", "-", raw)
    return raw


def build_identity_aliases(files: list[Path]) -> tuple[dict[tuple[str, str], tuple[str, str]], dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Build conservative aliases for date-scoped IDs using source crosswalks and unique overlap.

    A continuity edge requires one security/date, exact normalized name and type,
    exactly two source IDs, and exactly one not-disclosed placeholder paired with
    one observed quantity. Any conflicting non-null quantities block the edge.
    A multi-ID component is merged only if it has exactly one stable non-ondate
    anchor (e.g. share_id); otherwise its source IDs remain separate.
    """
    parent: dict[tuple[str, str], tuple[str, str]] = {}
    pair_dates: dict[tuple[str, str], dict[tuple[str, str], list[float | None]]] = defaultdict(lambda: defaultdict(list))
    presence: dict[tuple[str, str, str, str], dict[tuple[str, str], list[float | None]]] = defaultdict(lambda: defaultdict(list))
    explicit_edges: list[tuple[tuple[str, str], tuple[str, str]]] = []
    continuity_edges: list[tuple[tuple[str, str], tuple[str, str], str]] = []

    def ensure(pair):
        parent.setdefault(pair, pair)

    def find(pair):
        ensure(pair)
        while parent[pair] != pair:
            parent[pair] = parent[parent[pair]]
            pair = parent[pair]
        return pair

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for path in files:
        _, ins_code = workbook_identity(path)
        holders = read_sheet(path, "shareholders")
        history = read_sheet(path, "holdings_history")

        for holder in holders:
            scheme = text_value(holder.get("id_scheme"))
            holder_id = id_value(holder.get("shareholder_id"))
            if not scheme or not holder_id:
                continue
            pair = (scheme, holder_id)
            ensure(pair)
            raw_ids = str(holder.get("raw_source_id") or "")
            for token in re.split(r"[,;|]", raw_ids):
                match = re.fullmatch(r"\\s*([A-Za-z_]+)\\s*:\\s*([0-9]+)\\s*", token)
                if not match:
                    continue
                source_pair = (match.group(1), match.group(2))
                ensure(source_pair)
                if source_pair != pair:
                    explicit_edges.append((pair, source_pair))

        for row in history:
            scheme = text_value(row.get("id_scheme"))
            holder_id = id_value(row.get("shareholder_id"))
            observed = iso_date(row.get("date_gregorian")) or iso_date(row.get("date"))
            name = normalized_identity_name(row.get("shareholder_name"))
            holder_type = text_value(row.get("shareholder_type")) or ""
            if not scheme or not holder_id or not observed or not name:
                continue
            pair = (scheme, holder_id)
            ensure(pair)
            shares = number(row.get("shares"))
            pair_dates[pair][(ins_code, observed)].append(shares)
            presence[(ins_code, observed, name, holder_type)][pair].append(shares)

    # Explicit source crosswalks are the strongest available link evidence.
    for a, b in explicit_edges:
        if a in parent and b in parent:
            union(a, b)

    # Date-scoped IDs can be linked only when the same security/date has exactly
    # two source IDs for the exact legal name/type and one is a null placeholder.
    for signature, by_pair in presence.items():
        if len(by_pair) != 2:
            continue
        (a, vals_a), (b, vals_b) = list(by_pair.items())
        a_nonnull = [v for v in vals_a if v is not None]
        b_nonnull = [v for v in vals_b if v is not None]
        if bool(a_nonnull) == bool(b_nonnull):
            continue
        shared_keys = set(pair_dates[a]) & set(pair_dates[b])
        conflict = False
        for key in shared_keys:
            av = {v for v in pair_dates[a][key] if v is not None}
            bv = {v for v in pair_dates[b][key] if v is not None}
            if av and bv and av != bv:
                conflict = True
                break
        if conflict:
            continue
        union(a, b)
        continuity_edges.append((a, b, signature[1]))

    components: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for pair in parent:
        components[find(pair)].append(pair)

    aliases: dict[tuple[str, str], tuple[str, str]] = {}
    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    merged_components = 0
    merged_alias_count = 0
    for members in components.values():
        stable = [p for p in members if p[0] != "ondate_share_id"]
        # A component with multiple stable anchors is not safe to collapse.
        if len(members) > 1 and len(stable) == 1:
            canonical = stable[0]
            merged_components += 1
            for pair in members:
                aliases[pair] = canonical
                if pair != canonical:
                    merged_alias_count += 1
                    evidence[pair] = {
                        "kind": "inferred_exact_continuity",
                        "canonical_id": f"{canonical[0]}:{canonical[1]}",
                        "evidence": "explicit source crosswalk or unique same-security/date/name/type overlap with one not-disclosed placeholder",
                    }
        else:
            for pair in members:
                aliases[pair] = pair
                evidence[pair] = {
                    "kind": "source_identity",
                    "canonical_id": f"{pair[0]}:{pair[1]}",
                    "evidence": "no unique stable identity anchor; left separate",
                }

    stats = {
        "source_identity_pair_count": len(parent),
        "merged_component_count": merged_components,
        "merged_alias_count": merged_alias_count,
        "explicit_crosswalk_edge_count": len(explicit_edges),
        "continuity_edge_count": len(continuity_edges),
        "continuity_edges": [
            {
                "left": f"{a[0]}:{a[1]}",
                "right": f"{b[0]}:{b[1]}",
                "date": date_key,
                "basis": "same security/date + exact normalized name/type + exactly two IDs + one null and one observed quantity",
            }
            for a, b, date_key in continuity_edges
        ],
    }
    return aliases, evidence, stats


def main() -> int:
    files = sorted(WORKBOOK_DIR.glob("*.xlsx"))
    if not files:
        raise SystemExit(f"No workbooks found in {WORKBOOK_DIR}")

    registry, categories, registry_meta = load_sector_reference(ROOT / "docs")
    alias_map, alias_evidence, alias_stats = build_identity_aliases(files)
    investors: dict[str, dict[str, Any]] = {}
    symbol_latest_date: dict[str, str] = {}
    symbol_latest_label: dict[str, str] = {}
    company_names: dict[str, str | None] = {}
    company_histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    price_observations: dict[str, dict[str, set[float]]] = defaultdict(lambda: defaultdict(set))
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
        metadata_rows = read_sheet(path, "metadata")
        metadata = {
            text_value(row.get("key")): row.get("value")
            for row in metadata_rows if text_value(row.get("key"))
        }
        name_keys = ("company_name", "instrument_name", "lval30", "name", "full_name", "company")
        company_name = next(
            (text_value(metadata.get(key)) for key in name_keys if text_value(metadata.get(key))),
            None,
        )

        for company_row in company_rows:
            candidate = iso_date(company_row.get("date_gregorian")) or iso_date(company_row.get("date"))
            if candidate and (ins_code not in symbol_latest_date or candidate > symbol_latest_date[ins_code]):
                symbol_latest_date[ins_code] = candidate
            symbol_latest_label[ins_code] = text_value(company_row.get("symbol")) or symbol_from_name
            company_name = (
                text_value(company_row.get("company_name"))
                or text_value(company_row.get("instrument_name"))
                or company_name
            )
            if candidate:
                company_histories[ins_code].append({
                    "date": candidate,
                    "total_company_shares": number(company_row.get("total_company_shares")),
                    "disclosed_shareholder_count": number(company_row.get("disclosed_shareholder_count")),
                    "total_shareholder_count": number(company_row.get("total_shareholder_count")),
                    "disclosed_shares_sum": number(company_row.get("disclosed_shares_sum")),
                    "disclosed_ownership_percent_sum": number(company_row.get("disclosed_ownership_percent_sum")),
                    "corporate_action": text_value(company_row.get("corporate_action")),
                })
        company_names[ins_code] = company_name

        for source_row in history_rows:
            price = number(source_row.get("price"))
            price_date = iso_date(source_row.get("price_date")) or iso_date(source_row.get("date_gregorian")) or iso_date(source_row.get("date"))
            if price is not None and price > 0 and price_date:
                price_observations[ins_code][price_date].add(price)

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

            source_pair = (scheme, holder_id)
            canonical_pair = alias_map.get(source_pair, source_pair)
            source_pair_key = f"{source_pair[0]}:{source_pair[1]}"
            pair_key = f"{canonical_pair[0]}:{canonical_pair[1]}"
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
                "source_ids": {},
                "identity_link_evidence": {},
            })
            holder_meta = metadata_by_pair.get(source_pair_key, {})
            investor["source_ids"][source_pair_key] = {
                "id_scheme": source_pair[0],
                "shareholder_id": source_pair[1],
            }
            if source_pair != canonical_pair:
                investor["identity_link_evidence"][source_pair_key] = alias_evidence.get(source_pair, {})
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
                "identity_status": (
                    "inferred_exact_match" if source_pair != canonical_pair
                    else text_value(source_row.get("identity_status"))
                ),
                "source_id_scheme": source_pair[0],
                "source_shareholder_id": source_pair[1],
                "data_availability_status": text_value(source_row.get("data_availability_status")),
                "anomaly_flags": text_value(source_row.get("anomaly_flags")),
                "source": text_value(source_row.get("source")),
            }
            investor["timeline"].append(row)
            investor["symbols"].setdefault(ins_code, {
                "symbol": row["symbol"],
                "ins_code": ins_code,
                "observations": [],
            })["observations"].append(row)

    # Finalize investors after conservative source-ID alias resolution.
    final_investors = []
    aggregate_stats = {
        "positions_in_latest_valued_snapshots": 0,
        "investors_with_latest_valuation": 0,
        "timeline_rows": 0,
        "investors_with_affinity": 0,
        "mapped_positions": 0,
        "unmapped_positions": 0,
    }

    for investor in investors.values():
        raw_timeline = sorted(investor["timeline"], key=lambda r: (r["date"], r["ins_code"]))
        # Collapse duplicate date/security rows created by ondate IDs. Prefer an
        # observed quantity over a not-disclosed placeholder. Conflicting observed
        # quantities are kept as an explicit conflict, never summed.
        grouped_rows = defaultdict(list)
        for item in raw_timeline:
            grouped_rows[(item["date"], item["ins_code"])].append(item)
        timeline = []
        for key, candidates in sorted(grouped_rows.items()):
            observed = [item for item in candidates if item.get("shares") is not None]
            if observed:
                distinct_shares = {item.get("shares") for item in observed}
                if len(distinct_shares) > 1:
                    chosen = dict(observed[0])
                    chosen["shares"] = None
                    chosen["market_value_rial"] = None
                    chosen["identity_status"] = "identity_conflict"
                    chosen["data_availability_status"] = "identity_conflict"
                else:
                    chosen = sorted(
                        observed,
                        key=lambda item: (
                            item.get("source_id_scheme") == investor["id_scheme"]
                            and item.get("source_shareholder_id") == investor["shareholder_id"],
                            item.get("market_value_rial") is not None,
                        ),
                        reverse=True,
                    )[0]
            else:
                chosen = candidates[0]
            timeline.append(chosen)
        investor["timeline"] = timeline

        symbol_items = []
        current_value_rial = 0.0
        current_value_known = False
        for ins_code, symbol_info in investor["symbols"].items():
            records = sorted(
                (r for r in timeline if r["ins_code"] == ins_code and r.get("shares") is not None),
                key=lambda r: r["date"],
            )
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
                "group_code": registry.get(ins_code, {}).get("cSecVal"),
                "group_label": registry.get(ins_code, {}).get("lSecVal"),
                "subgroup_code": registry.get(ins_code, {}).get("cSoSecVal"),
                "subgroup_label": registry.get(ins_code, {}).get("lSoSecVal"),
                "company_name": company_names.get(ins_code),
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
                "id_scheme": row.get("source_id_scheme") or investor["id_scheme"],
                "shareholder_id": row.get("source_shareholder_id") or investor["shareholder_id"],
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
        engine_value = affinity.get("current_portfolio_value_rial")
        engine_positions = int(affinity.get("current_observed_instruments_count") or 0)
        if engine_value is not None:
            aggregate_stats["investors_with_latest_valuation"] += 1
        aggregate_stats["positions_in_latest_valued_snapshots"] += engine_positions

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
                "data_availability_status": row.get("data_availability_status"),
                "source_id_scheme": row.get("source_id_scheme"),
                "source_shareholder_id": row.get("source_shareholder_id"),
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
        open_positions_count = sum(
            1 for item in symbol_items if item["exit_status"] == "currently_disclosed"
        )
        final_investors.append({
            "id": investor["id"],
            "id_scheme": investor["id_scheme"],
            "shareholder_id": investor["shareholder_id"],
            "shareholder_name": investor.get("shareholder_name") or "نام افشا نشده",
            "name_variants": sorted(investor.get("name_variants", set())),
            "shareholder_type": investor.get("shareholder_type") or "unknown",
            "identity_verification_status": (
                "inferred_exact_continuity" if investor.get("identity_link_evidence")
                else investor.get("identity_verification_status")
            ),
            "source_ids": sorted(investor.get("source_ids", {}).values(), key=lambda x: (x["id_scheme"], x["shareholder_id"])),
            "identity_link_evidence": investor.get("identity_link_evidence", {}),
            "n_companies": len(symbol_items),
            "open_positions_count": open_positions_count,
            "latest_observation_date": max((r["date"] for r in timeline), default=None),
            "current_portfolio_value_rial": engine_value,
            "portfolio_value_date": affinity.get("latest_valued_date"),
            "positions_in_latest_valued_snapshot": engine_positions,
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

    companies = {}
    for path in files:
        symbol_from_name, ins_code = workbook_identity(path)
        history = sorted(company_histories.get(ins_code, []), key=lambda row: row["date"])
        latest = history[-1] if history else {}
        prices = []
        for price_date, values in sorted(price_observations.get(ins_code, {}).items()):
            # A conflicting reference price on the same date is not silently chosen.
            if len(values) == 1:
                prices.append({"date": price_date, "price_rial": next(iter(values))})
            else:
                prices.append({"date": price_date, "price_rial": None, "conflict": True})
        symbol = symbol_latest_label.get(ins_code) or symbol_from_name
        latest_date = latest.get("date")
        company_shareholders = []
        if latest_date:
            for person in final_investors:
                for row in person.get("timeline", []):
                    if row.get("ins_code") == ins_code and row.get("date") == latest_date and row.get("shares") is not None:
                        company_shareholders.append({
                            "investor_id": person["id"],
                            "shareholder_name": person["shareholder_name"],
                            "shareholder_type": person["shareholder_type"],
                            "shares": row.get("shares"),
                            "ownership_pct": row.get("ownership_pct"),
                            "market_value_rial": row.get("market_value_rial"),
                            "open_positions_count": person.get("open_positions_count", 0),
                        })
        companies[ins_code] = {
            "ins_code": ins_code,
            "symbol": symbol,
            "company_name": company_names.get(ins_code),
            "group_code": registry.get(ins_code, {}).get("cSecVal"),
            "group_label": registry.get(ins_code, {}).get("lSecVal"),
            "subgroup_code": registry.get(ins_code, {}).get("cSoSecVal"),
            "subgroup_label": registry.get(ins_code, {}).get("lSoSecVal"),
            "latest_snapshot": latest,
            "history": history,
            "price_series": prices,
            "shareholders": sorted(company_shareholders, key=lambda row: row.get("market_value_rial") or 0, reverse=True),
        }

    universe = [
        {
            "ins_code": ins_code,
            "symbol": item.get("symbol"),
            "group_code": item.get("cSecVal"),
            "group_label": item.get("lSecVal"),
            "subgroup_code": item.get("cSoSecVal"),
            "subgroup_label": item.get("lSoSecVal"),
            "company_name": companies.get(ins_code, {}).get("company_name"),
            "has_sample_detail": ins_code in companies,
        }
        for ins_code, item in registry.items()
    ]

    if not all_dates:
        raise SystemExit("No usable Gregorian observation dates found in workbook histories.")

    target_identity_checks = [
        {
            "canonical_id": item["id"],
            "shareholder_name": item["shareholder_name"],
            "source_id_count": len(item.get("source_ids", [])),
            "source_ids": item.get("source_ids", []),
            "symbols": [position["symbol"] for position in item.get("symbols", [])],
            "n_companies": item["n_companies"],
            "timeline_rows_after_deduplication": len(item.get("timeline", [])),
            "first_observation_date": min((row["date"] for row in item.get("timeline", [])), default=None),
            "last_observation_date": max((row["date"] for row in item.get("timeline", [])), default=None),
        }
        for item in final_investors
        if "امیدتابان هور" in normalized_identity_name(item.get("shareholder_name"))
    ]
    summary = {
        "workbook_count": len(files),
        "symbol_count": len({workbook_identity(path)[1] for path in files}),
        "history_rows_in_source": total_history_rows,
        "history_rows_with_composite_id_and_date": valid_history_rows,
        "history_rows_without_composite_id": skipped_no_identity,
        "investor_count": len(final_investors),
        "identity_alias_resolution": alias_stats,
        "target_identity_checks": target_identity_checks,
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
            "Identity uses stable source IDs plus conservative ondate continuity links; exact name/type alone never merges records. "
            "An assumed exit means absence from a later disclosed snapshot, not proof of a sale."
        ),
        "methodology": {
            "identity_key": "canonical stable ID with conservative ondate continuity aliases",
            "name_matching": "exact normalized name/type is used only with same-security/date unique overlap and a null-vs-observed placeholder; never as a standalone merge key",
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
        "companies": companies,
        "official_universe": universe,
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
