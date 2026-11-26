import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DATA = ROOT / "docs" / "data" / "analytics.json"


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_pages_dataset_contains_only_the_six_expected_symbols():
    data = json.loads(PUBLIC_DATA.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in data["symbols"]] == [
        "شكبير", "كاريز", "زشك", "شگامرن", "آواپارس", "فنقره"
    ]
    assert data["summary"]["symbols"] == 6
    assert data["summary"]["observations"] == 180


def test_public_dataset_does_not_contain_raw_identity_or_holding_fields():
    data = json.loads(PUBLIC_DATA.read_text(encoding="utf-8"))
    keys = set(_walk_keys(data))
    forbidden = {
        "shareholder_id", "shareholder_name", "raw_source_id", "id_scheme",
        "holder_id", "share_id", "ondate_share_id", "canonical_name",
        "confirmed_positions", "probable_positions", "uncertain_candidates",
        "holding_value_rial", "shares_change", "identity_evidence",
    }
    assert not (keys & forbidden)
    assert not any(path.suffix.lower() == ".xlsx" for path in (ROOT / "docs").rglob("*"))


def test_missing_holder_observations_are_not_published_as_zero_percent():
    data = json.loads(PUBLIC_DATA.read_text(encoding="utf-8"))
    for symbol in data["symbols"]:
        for date, disclosed_pct, major_holder_count, status in symbol["series"]:
            if status == "N":
                assert disclosed_pct is None
                assert major_holder_count == 0


def test_over_one_hundred_percent_history_is_flagged_not_rewritten():
    data = json.loads(PUBLIC_DATA.read_text(encoding="utf-8"))
    kariz = next(item for item in data["symbols"] if item["symbol"] == "كاريز")
    flagged = [row[1] for row in kariz["series"] if row[3] == "X"]
    assert flagged == [107.97, 107.964]
    assert kariz["quality"][3] == 2


def test_pages_preview_includes_the_privacy_consent_analytics_bundle():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    client_path = ROOT / "docs" / "analytics-client.js"
    assert client_path.is_file()
    assert '<script defer src="./analytics-client.js"></script>' in html
    assert 'privacy-analytics-panel' in html
    assert '/api/v1/analytics/consent' in html

    client = client_path.read_text(encoding="utf-8")
    assert "TloombergAnalytics" in client
    assert "/api/v1/analytics/events" in client
    assert "page_view" in client
    assert "active_time" in client
    assert "scroll" in client
    assert "data-analytics-exclude" in client
    # The client must not persist event queues or inspect user-entered/rendered text.
    assert "localStorage" not in client
    assert "sessionStorage" not in client
    assert "document.cookie" not in client
    assert "innerText" not in client
    assert "textContent" not in client
    assert "outerHTML" not in client


def test_admin_ui_exposes_aggregate_and_superadmin_user_behavior_reports():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert "/api/v1/analytics/admin/overview" in html
    assert "/api/v1/analytics/admin/paths" in html
    assert "/api/v1/analytics/admin/users/" in html
    assert "analyticsUserEvents" in html
    assert "تحلیل رفتار کاربران" in html
    assert "تاریخچهٔ رفتار یک کاربر" in html
