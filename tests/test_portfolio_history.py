"""Tests for the dated shareholder portfolio history and exit convention."""

from server import (
    build_portfolio_history_summary,
    classify_historical_exit,
    classify_portfolio_change,
    ownership_percent_for_date,
)


def _row(date, ins_code, shares, **extra):
    row = {
        "date": date,
        "jalali_date": date,
        "ins_code": ins_code,
        "symbol": ins_code,
        "id_scheme": "share_id",
        "shareholder_id": 123,
        "shares": shares,
        "shares_change": None,
        "change_type": None,
        "change_calculation_status": None,
        "corporate_action": None,
        "reference_price": 100 if shares is not None else None,
        "reference_price_date": date if shares is not None else None,
        "total_company_shares": 1000,
        "cisin": None,
        "identity_conflict": False,
    }
    row.update(extra)
    return row


def test_old_security_remains_in_history_after_latest_disclosure_disappears():
    rows = [
        _row("2024-01-01", "OLD", 250),
        _row("2024-01-02", "OLD", 250, shares_change=0,
             change_calculation_status="consecutive_observations"),
    ]
    summary, symbols, series = build_portfolio_history_summary(rows)
    assert summary["symbols_count"] == 1
    assert symbols[0]["symbol"] == "OLD"
    assert symbols[0]["first_observed_shares"] == 250
    assert symbols[0]["last_observation_shares"] == 250
    assert summary["observations_count"] == 2
    assert series[-1]["combined_value_rial"] == 25_000


def test_missing_disclosure_is_not_a_zero_balance_or_a_sell_event():
    row = _row("2024-01-03", "OLD", None,
               change_calculation_status="not_disclosed_on_date")
    assert classify_portfolio_change(row) == "not_disclosed"
    summary, symbols, series = build_portfolio_history_summary([
        _row("2024-01-02", "OLD", 250), row
    ])
    assert symbols[0]["last_observation_shares"] is None
    assert symbols[0]["last_known_shares"] == 250
    assert series[-1]["combined_value_rial"] is None


def test_exit_convention_requires_a_later_company_snapshot():
    assert classify_historical_exit("2024-01-02", "2024-01-03") == "assumed_sold"
    assert classify_historical_exit("2024-01-03", "2024-01-03") == "currently_disclosed"
    assert classify_historical_exit("2024-01-03", None) == "unknown"
    assert classify_historical_exit(None, "2024-01-03") == "unknown"


def test_net_quantity_changes_are_not_execution_proof():
    assert classify_portfolio_change({
        "shares_change": 500,
        "change_calculation_status": "consecutive_observations",
    }) == "net_increase"
    assert classify_portfolio_change({
        "shares_change": -200,
        "change_calculation_status": "consecutive_observations",
    }) == "net_decrease"


def test_first_observation_and_capital_action_precedence():
    assert classify_portfolio_change({
        "shares_change": 100,
        "change_calculation_status": "first_observation_no_previous",
    }) == "first_observation"
    assert classify_portfolio_change({
        "shares_change": 100,
        "corporate_action": "capital_increase",
        "change_calculation_status": "consecutive_observations",
    }) == "capital_action"


def test_linked_identity_quantity_conflict_is_not_valued():
    rows = [
        _row("2024-01-01", "X", 10, id_scheme="share_id", shareholder_id=1),
        _row("2024-01-01", "X", 12, id_scheme="holder_id", shareholder_id=2),
        _row("2024-01-01", "Y", 10),
    ]
    summary, symbols, series = build_portfolio_history_summary(rows)
    x = next(item for item in symbols if item["symbol"] == "X")
    assert x["identity_conflicts_count"] == 1
    assert x["known_shares_observations_count"] == 0
    assert series[0]["combined_value_rial"] == 1000
    assert summary["identity_conflicts_count"] == 1


def test_same_cisin_is_counted_once_in_portfolio_value_per_date():
    rows = [
        _row("2024-01-01", "X1", 10, cisin="ISIN-A"),
        _row("2024-01-01", "X2", 10, cisin="ISIN-A"),
    ]
    summary, symbols, series = build_portfolio_history_summary(rows)
    assert summary["symbols_count"] == 2
    assert series[0]["combined_value_rial"] == 1000
    assert series[0]["observed_instruments_count"] == 1


def test_same_date_ownership_percentage_requires_a_positive_denominator():
    assert ownership_percent_for_date(25, 100) == 25.0
    assert ownership_percent_for_date(25, None) is None
    assert ownership_percent_for_date(25, 0) is None
