"""Tests for full-history investor portfolio reconstruction semantics."""
from server import build_portfolio_history_summary, classify_portfolio_change


def _row(date, symbol, shares, **extra):
    row = {
        "date": date, "jalali_date": date, "ins_code": symbol, "symbol": symbol,
        "shares": shares, "shares_change": None,
        "change_type": "unknown" if shares is None else "new",
        "change_calculation_status": (
            "not_disclosed_on_date" if shares is None else "first_observation_no_previous"),
        "corporate_action": None,
        "reference_price": 100 if shares is not None else None,
        "reference_price_date": date if shares is not None else None,
        "total_company_shares": 1000, "cisin": None, "identity_conflict": False,
    }
    row.update(extra)
    return row


def test_disappeared_security_stays_in_historical_universe():
    rows = [
        _row("2024-01-01", "OLD", 250),
        _row("2024-01-02", "OLD", 250, shares_change=0, change_type="unchanged",
             change_calculation_status="consecutive_observations"),
        _row("2024-01-03", "OLD", None, change_type="unknown",
             change_calculation_status="not_disclosed_on_date"),
    ]
    summary, symbols, series = build_portfolio_history_summary(rows)
    assert summary["symbols_count"] == 1
    assert symbols[0]["symbol"] == "OLD"
    assert symbols[0]["first_observed_shares"] == 250
    assert symbols[0]["last_observation_shares"] is None
    assert symbols[0]["last_known_shares"] == 250
    assert summary["observations_count"] == 3
    assert series[-1]["combined_value_rial"] is None


def test_quantity_increase_and_decrease_are_not_trade_actions():
    assert classify_portfolio_change({
        "shares_change": 500, "change_calculation_status": "consecutive_observations"
    }) == "net_increase"
    assert classify_portfolio_change({
        "shares_change": -200, "change_calculation_status": "consecutive_observations"
    }) == "net_decrease"


def test_first_observation_and_capital_action_have_precedence():
    assert classify_portfolio_change({
        "shares_change": 100, "change_calculation_status": "first_observation_no_previous"
    }) == "first_observation"
    assert classify_portfolio_change({
        "shares_change": 100, "corporate_action": "capital_increase",
        "change_calculation_status": "consecutive_observations"
    }) == "capital_action"


def test_identity_conflict_is_excluded_from_value():
    rows = [
        _row("2024-01-01", "X", None, identity_conflict=True,
             shares_change=None, change_type="increase"),
        _row("2024-01-01", "Y", 10),
    ]
    summary, symbols, series = build_portfolio_history_summary(rows)
    x = next(item for item in symbols if item["symbol"] == "X")
    assert x["identity_conflicts_count"] == 1
    assert x["known_shares_observations_count"] == 0
    assert series[0]["combined_value_rial"] == 1000


def test_same_cisin_is_counted_once_per_date():
    rows = [
        _row("2024-01-01", "X1", 10, cisin="ISIN-A"),
        _row("2024-01-01", "X2", 10, cisin="ISIN-A"),
    ]
    summary, symbols, series = build_portfolio_history_summary(rows)
    assert summary["symbols_count"] == 2
    assert series[0]["combined_value_rial"] == 1000
    assert series[0]["observed_instruments_count"] == 1
