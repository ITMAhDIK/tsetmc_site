"""Sector allocation, historical affinity and net-rotation engine."""
from __future__ import annotations
import json
import math
import os
from datetime import date, timedelta


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _date(value):
    if value is None:
        return None
    raw = str(value).strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        raw = "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:])
    try:
        return date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        return None


def _code(value):
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _has_text(value):
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return str(value).strip().lower() not in ("", "0", "none", "null", "false", "[]", "{}")


def _has_anomaly(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "[]", "{}", "null", "none")
    return bool(value)


def load_sector_reference(site_dir):
    """Load official TSETMC instrument metadata and explicit dashboard mappings."""
    data_dir = os.path.join(os.path.abspath(site_dir), "data")
    instrument_path = os.path.join(data_dir, "instrument_sectors.json")
    mapping_path = os.path.join(data_dir, "dashboard_sector_categories.json")
    instrument_doc = {}
    mapping_doc = {}
    if os.path.isfile(instrument_path):
        with open(instrument_path, "r", encoding="utf-8") as handle:
            instrument_doc = json.load(handle)
    if os.path.isfile(mapping_path):
        with open(mapping_path, "r", encoding="utf-8") as handle:
            mapping_doc = json.load(handle)

    categories = {}
    for item in mapping_doc.get("categories", []):
        if isinstance(item, dict):
            category_id = _code(item.get("id"))
            label = str(item.get("label") or "").strip()
            if category_id and label:
                categories[category_id] = label
    group_map = mapping_doc.get("official_group_to_category", {})
    sub_map = mapping_doc.get("official_subsector_to_category", {})
    if not isinstance(group_map, dict):
        group_map = {}
    if not isinstance(sub_map, dict):
        sub_map = {}

    raw_items = instrument_doc.get("instruments", {})
    if not isinstance(raw_items, dict):
        raw_items = {}
    registry = {}
    for raw_code, raw in raw_items.items():
        if not isinstance(raw, dict):
            continue
        ins_code = _code(raw.get("ins_code") or raw_code)
        if not ins_code:
            continue
        group_code = _code(raw.get("cSecVal"))
        subgroup_code = _code(raw.get("cSoSecVal"))
        category_id = None
        if group_code and subgroup_code:
            category_id = _code(sub_map.get(group_code + ":" + subgroup_code))
        if category_id is None and group_code:
            category_id = _code(group_map.get(group_code))
        registry[ins_code] = {
            "ins_code": ins_code,
            "cSecVal": group_code,
            "lSecVal": str(raw.get("lSecVal") or "").strip() or None,
            "cSoSecVal": subgroup_code,
            "lSoSecVal": str(raw.get("lSoSecVal") or "").strip() or None,
            "dashboard_category_id": category_id if category_id in categories else None,
            "dashboard_category_label": categories.get(category_id),
        }
    metadata = {
        "instrument_file_present": os.path.isfile(instrument_path),
        "category_file_present": os.path.isfile(mapping_path),
        "instrument_count": len(registry),
        "mapped_dashboard_category_count": sum(
            1 for item in registry.values() if item["dashboard_category_id"]),
        "categories_count": len(categories),
        "generated_at": instrument_doc.get("generated_at"),
        "source": instrument_doc.get("source"),
    }
    return registry, categories, metadata


class SectorAffinityAccumulator:
    """Accumulate sector metrics in one pass, buffering one observation date."""

    def __init__(self, registry, categories=None, requested_pair=None,
                 as_of_date=None, half_life_months=6.0,
                 max_price_age_days=62, rotation_lookback_days=90,
                 rotation_scale_pct=5.0):
        if not 1 <= float(half_life_months) <= 60:
            raise ValueError("half_life_months must be between 1 and 60")
        if not 0 <= int(max_price_age_days) <= 365:
            raise ValueError("max_price_age_days must be between 0 and 365")
        if not 1 <= int(rotation_lookback_days) <= 730:
            raise ValueError("rotation_lookback_days must be between 1 and 730")
        if float(rotation_scale_pct) <= 0:
            raise ValueError("rotation_scale_pct must be positive")
        self.registry = registry or {}
        self.categories = categories or {}
        self.requested_pair = requested_pair
        self.as_of_date = _date(as_of_date)
        self.half_life_months = float(half_life_months)
        self.max_price_age_days = int(max_price_age_days)
        self.rotation_lookback_days = int(rotation_lookback_days)
        self.rotation_scale_pct = float(rotation_scale_pct)
        self._current_date = None
        self._pending = []
        self._daily = []
        self._finished = False
        self._result = None
        self._counts = {
            "raw_observations": 0, "identity_conflicts": 0,
            "cisin_alias_conflicts": 0, "unique_security_observations": 0,
            "valued_positions": 0, "missing_quantity": 0,
            "missing_price": 0, "stale_or_future_price": 0,
            "rotation_change_candidates": 0, "rotation_eligible": 0,
            "rotation_excluded": 0,
        }

    def _identity_factor(self, row):
        pair = (row.get("id_scheme"), row.get("shareholder_id"))
        if self.requested_pair and pair == self.requested_pair:
            return 1.0
        status = row.get("identity_status")
        if status == "verified_same_source_row":
            return 0.95
        if status == "inferred_exact_match":
            return 0.70
        return 0.50

    def add(self, row):
        if self._finished:
            raise RuntimeError("cannot add observations after finish()")
        row_date = _date(row.get("date"))
        if row_date is None or (self.as_of_date and row_date > self.as_of_date):
            return
        if self._current_date is None:
            self._current_date = row_date
        elif row_date < self._current_date:
            raise ValueError("observations must be ordered by ascending date")
        elif row_date != self._current_date:
            self._finish_day()
            self._current_date = row_date
        self._pending.append(row)
        self._counts["raw_observations"] += 1

    def _finish_day(self):
        if not self._pending or self._current_date is None:
            return
        by_security = {}
        conflicts = set()
        observed_codes = set()
        for row in self._pending:
            ins_code = _code(row.get("ins_code"))
            if ins_code:
                observed_codes.add(ins_code)
            if row.get("identity_conflict"):
                self._counts["identity_conflicts"] += 1
            security_key = _code(row.get("cisin")) or ins_code
            if not security_key:
                continue
            shares = _number(row.get("shares"))
            price = _number(row.get("reference_price"))
            price_date = _date(row.get("reference_price_date"))
            age = (self._current_date - price_date).days if price_date else 0
            price_ok = price is not None and price > 0 and age >= 0 and age <= self.max_price_age_days
            value = (shares * price if shares is not None and shares >= 0
                     and price_ok and not row.get("identity_conflict") else None)
            candidate = {
                "row": row, "ins_code": ins_code, "shares": shares,
                "price": price, "price_ok": price_ok, "value": value,
                "identity_factor": self._identity_factor(row),
            }
            if security_key not in by_security:
                by_security[security_key] = candidate
                continue
            previous = by_security[security_key]
            same_shares = previous["shares"] == candidate["shares"]
            pval, nval = previous["value"], candidate["value"]
            same_value = (
                pval is None or nval is None
                or math.isclose(pval, nval, rel_tol=1e-9, abs_tol=0.01)
            )
            if not same_shares or not same_value:
                conflicts.add(security_key)
                continue
            if ((previous["value"] is None and candidate["value"] is not None)
                    or candidate["identity_factor"] > previous["identity_factor"]):
                by_security[security_key] = candidate

        group_values = {}
        subgroup_values = {}
        category_values = {}
        rotation_values = {}
        total_value = classified_value = 0.0
        valued_count = identity_sum = identity_count = 0
        rotation_candidates = rotation_eligible = rotation_excluded = 0

        for security_key, item in by_security.items():
            if security_key in conflicts:
                self._counts["cisin_alias_conflicts"] += 1
                continue
            self._counts["unique_security_observations"] += 1
            row = item["row"]
            if row.get("identity_conflict"):
                continue
            shares = item["shares"]
            if shares is None or shares < 0:
                self._counts["missing_quantity"] += 1
                continue
            if not item["price_ok"]:
                price_date = _date(row.get("reference_price_date"))
                if item["price"] is None or item["price"] <= 0:
                    self._counts["missing_price"] += 1
                elif price_date is None or price_date > self._current_date or (
                    self._current_date - price_date
                ).days > self.max_price_age_days:
                    self._counts["stale_or_future_price"] += 1
                continue

            value = float(item["value"] or 0.0)
            total_value += value
            valued_count += 1
            identity_sum += item["identity_factor"]
            identity_count += 1
            self._counts["valued_positions"] += 1

            sector = self.registry.get(item["ins_code"] or "", {})
            group_code = _code(sector.get("cSecVal"))
            if group_code:
                group = group_values.setdefault(group_code, {
                    "value": 0.0, "label": sector.get("lSecVal") or group_code,
                    "instruments": set(),
                })
                group["value"] += value
                if item["ins_code"]:
                    group["instruments"].add(item["ins_code"])
                classified_value += value
                subgroup_code = _code(sector.get("cSoSecVal"))
                if subgroup_code:
                    subkey = (group_code, subgroup_code)
                    subgroup_values[subkey] = subgroup_values.get(subkey, 0.0) + value
                category_id = _code(sector.get("dashboard_category_id"))
                category_label = sector.get("dashboard_category_label")
                if category_id and category_label:
                    cat = category_values.setdefault(category_id, {
                        "value": 0.0, "label": category_label,
                    })
                    cat["value"] += value

            delta = _number(row.get("shares_change"))
            status = row.get("change_calculation_status")
            if delta is not None:
                rotation_candidates += 1
                self._counts["rotation_change_candidates"] += 1
            eligible = (
                delta is not None
                and status == "consecutive_observations"
                and not _has_text(row.get("corporate_action"))
                and not _has_anomaly(row.get("anomaly_flags"))
                and not row.get("identity_conflict")
                and item["price_ok"]
                and group_code is not None
            )
            if eligible:
                rotation_eligible += 1
                self._counts["rotation_eligible"] += 1
                rotation_values[group_code] = rotation_values.get(group_code, 0.0) + delta * item["price"]
            elif delta is not None:
                rotation_excluded += 1
                self._counts["rotation_excluded"] += 1

        self._daily.append({
            "date": self._current_date.isoformat(),
            "total_value": total_value,
            "classified_value": classified_value,
            "group_values": {key: value["value"] for key, value in group_values.items()},
            "group_labels": {key: value["label"] for key, value in group_values.items()},
            "group_instruments": {key: len(value["instruments"]) for key, value in group_values.items()},
            "subgroup_values": {
                key[0] + ":" + key[1]: value for key, value in subgroup_values.items()
            },
            "category_values": {key: value["value"] for key, value in category_values.items()},
            "category_labels": {key: value["label"] for key, value in category_values.items()},
            "rotation_values": rotation_values,
            "observed_instruments": len(observed_codes),
            "unique_securities": len(by_security),
            "valued_positions": valued_count,
            "identity_confidence": identity_sum / identity_count if identity_count else 0.0,
            "rotation_candidates": rotation_candidates,
            "rotation_eligible": rotation_eligible,
            "rotation_excluded": rotation_excluded,
        })
        self._pending = []

    def finish(self):
        if self._finished:
            return
        self._finish_day()
        self._finished = True
        self._build_result()

    def _build_result(self):
        days = self._daily
        latest_day = next((item for item in reversed(days) if item["total_value"] > 0), None)
        latest_date = latest_day["date"] if latest_day else (days[-1]["date"] if days else None)
        latest_valued_date = latest_day["date"] if latest_day else None

        group_codes = set()
        category_ids = set()
        for item in days:
            group_codes.update(item["group_values"])
            category_ids.update(item["category_values"])
        weighted_groups = {key: 0.0 for key in group_codes}
        weighted_categories = {key: 0.0 for key in category_ids}
        decay_total = 0.0
        if latest_valued_date:
            anchor = _date(latest_valued_date)
            for item in days:
                if item["total_value"] <= 0:
                    continue
                age_months = max(0.0, (anchor - _date(item["date"])).days / 30.4375)
                decay = 2.0 ** (-age_months / self.half_life_months)
                decay_total += decay
                for code in group_codes:
                    weighted_groups[code] += (
                        100.0 * item["group_values"].get(code, 0.0) / item["total_value"] * decay
                    )
                for category_id in category_ids:
                    weighted_categories[category_id] += (
                        100.0 * item["category_values"].get(category_id, 0.0)
                        / item["total_value"] * decay
                    )

        cutoff = _date(latest_valued_date) - timedelta(days=self.rotation_lookback_days) if latest_valued_date else None
        recent_rotation = {}
        recent_candidates = recent_eligible = recent_excluded = 0
        for item in days:
            item_date = _date(item["date"])
            if cutoff is None or item_date < cutoff or item_date > _date(latest_valued_date):
                continue
            for code, value in item["rotation_values"].items():
                recent_rotation[code] = recent_rotation.get(code, 0.0) + value
            recent_candidates += item["rotation_candidates"]
            recent_eligible += item["rotation_eligible"]
            recent_excluded += item["rotation_excluded"]

        counts = self._counts
        valuation_coverage = (
            counts["valued_positions"] / counts["unique_security_observations"]
            if counts["unique_security_observations"] else 0.0
        )
        classification_coverage = (
            latest_day["classified_value"] / latest_day["total_value"]
            if latest_day and latest_day["total_value"] > 0 else 0.0
        )
        identity_confidence = latest_day["identity_confidence"] if latest_day else 0.0
        rotation_coverage = recent_eligible / recent_candidates if recent_candidates else 0.0
        confidence = 100.0 * (
            0.35 * min(1.0, valuation_coverage)
            + 0.30 * min(1.0, classification_coverage)
            + 0.20 * min(1.0, identity_confidence)
            + 0.15 * min(1.0, rotation_coverage)
        )

        def make_group(code, label, current_value, historical_pct, rotation_value, observations):
            total = latest_day["total_value"] if latest_day else 0.0
            current_pct = 100.0 * current_value / total if total > 0 else 0.0
            rotation_pct = 100.0 * rotation_value / total if total > 0 else 0.0
            rotation_score = 50.0 + 50.0 * math.tanh(rotation_pct / self.rotation_scale_pct)
            interest = 0.50 * current_pct + 0.30 * historical_pct + 0.20 * rotation_score
            direction = "increasing" if rotation_value > 0.01 else (
                "decreasing" if rotation_value < -0.01 else "neutral"
            )
            return {
                "code": code, "label": label,
                "current_value_rial": round(current_value, 2),
                "current_weight_pct": round(current_pct, 6),
                "historical_affinity_pct": round(historical_pct, 6),
                "recent_net_change_value_rial": round(rotation_value, 2),
                "recent_net_change_pct_of_portfolio": round(rotation_pct, 6),
                "rotation_direction": direction,
                "rotation_score": round(rotation_score, 3),
                "interest_score": round(interest, 3),
                "confidence_score": round(confidence, 3),
                "observations_count": observations,
            }

        official_groups = []
        for code in group_codes:
            current_value = latest_day["group_values"].get(code, 0.0) if latest_day else 0.0
            labels = [item["group_labels"][code] for item in days if code in item["group_labels"]]
            observations = sum(1 for item in days if code in item["group_values"])
            group = make_group(
                code, labels[-1] if labels else code, current_value,
                weighted_groups[code] / decay_total if decay_total else 0.0,
                recent_rotation.get(code, 0.0), observations,
            )
            subgroup_current = {}
            if latest_day:
                for key, value in latest_day["subgroup_values"].items():
                    if key.startswith(code + ":"):
                        subgroup_code = key.split(":", 1)[1]
                        subgroup_current[subgroup_code] = value
            group["subgroups"] = [
                {
                    "code": sub_code,
                    "label": next((
                        sector.get("lSoSecVal") or sub_code
                        for sector in self.registry.values()
                        if _code(sector.get("cSecVal")) == code
                        and _code(sector.get("cSoSecVal")) == sub_code
                    ), sub_code),
                    "current_value_rial": round(value, 2),
                    "current_weight_pct": round(
                        100.0 * value / current_value if current_value > 0 else 0.0, 6
                    ),
                }
                for sub_code, value in sorted(subgroup_current.items())
            ]
            official_groups.append(group)
        official_groups.sort(key=lambda item: item["interest_score"], reverse=True)

        dashboard_categories = []
        for category_id in category_ids:
            current_value = latest_day["category_values"].get(category_id, 0.0) if latest_day else 0.0
            labels = [item["category_labels"][category_id] for item in days if category_id in item["category_labels"]]
            observations = sum(1 for item in days if category_id in item["category_values"])
            dashboard_categories.append(make_group(
                category_id, labels[-1] if labels else self.categories.get(category_id, category_id),
                current_value,
                weighted_categories[category_id] / decay_total if decay_total else 0.0,
                0.0, observations,
            ))
        dashboard_categories.sort(key=lambda item: item["interest_score"], reverse=True)

        history = []
        for item in days:
            total = item["total_value"]
            history.append({
                "date": item["date"],
                "total_value_rial": round(total, 2) if total > 0 else None,
                "observed_instruments_count": item["observed_instruments"],
                "valued_instruments_count": item["valued_positions"],
                "official_group_weights": {
                    code: round(value * 100.0 / total, 6)
                    for code, value in item["group_values"].items()
                } if total > 0 else {},
            })

        registry_missing = not self.registry
        status = "registry_missing" if registry_missing else (
            "ready" if latest_day else "no_valued_history"
        )
        warnings = []
        if registry_missing:
            warnings.append("instrument_sector_registry_missing")
        if classification_coverage < 0.95:
            warnings.append("some_valued_positions_are_not_mapped_to_official_groups")
        if not recent_candidates:
            warnings.append("no_eligible_net_change_evidence_in_rotation_window")
        if counts["identity_conflicts"]:
            warnings.append("identity_conflicts_excluded")
        if counts["cisin_alias_conflicts"]:
            warnings.append("conflicting_cisin_aliases_excluded")

        self._result = {
            "status": status,
            "latest_observation_date": latest_date,
            "latest_valued_date": latest_valued_date,
            "current_portfolio_value_rial": round(latest_day["total_value"], 2) if latest_day else None,
            "current_observed_instruments_count": latest_day["observed_instruments"] if latest_day else 0,
            "half_life_months": self.half_life_months,
            "rotation_lookback_days": self.rotation_lookback_days,
            "score_weights": {
                "current_allocation": 0.50,
                "historical_affinity": 0.30,
                "recent_net_rotation": 0.20,
            },
            "official_groups": official_groups,
            "dashboard_categories": dashboard_categories,
            "history": history,
            "data_quality": {
                "confidence_score": round(confidence, 3),
                "valuation_coverage_pct": round(100.0 * valuation_coverage, 3),
                "sector_classification_coverage_pct": round(100.0 * classification_coverage, 3),
                "identity_confidence_pct": round(100.0 * identity_confidence, 3),
                "rotation_evidence_coverage_pct": round(100.0 * rotation_coverage, 3),
                "raw_observations": counts["raw_observations"],
                "unique_security_observations": counts["unique_security_observations"],
                "valued_positions": counts["valued_positions"],
                "missing_quantity": counts["missing_quantity"],
                "missing_price": counts["missing_price"],
                "stale_or_future_price": counts["stale_or_future_price"],
                "identity_conflicts": counts["identity_conflicts"],
                "cisin_alias_conflicts": counts["cisin_alias_conflicts"],
                "rotation_change_candidates": recent_candidates,
                "rotation_eligible": recent_eligible,
                "rotation_excluded": recent_excluded,
            },
            "warnings": warnings,
            "methodology": {
                "current_allocation": "same-date disclosed shares x valid reference price; no carry-forward",
                "historical_affinity": "exponentially decayed average of observed sector allocation",
                "recent_net_rotation": "consecutive net share-count change x observation-date reference price; excludes corporate actions, gaps and anomalies",
                "trade_proof": False,
                "identity_rule": "existing linked identity resolver; no name-only matching",
                "cisin_rule": "one economic position per CISIN and date; conflicting aliases excluded",
                "dashboard_mapping_rule": "explicit official-code mapping only",
            },
        }

    def result(self):
        self.finish()
        return self._result or {}
