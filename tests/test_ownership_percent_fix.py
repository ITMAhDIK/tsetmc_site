import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_server():
    spec = importlib.util.spec_from_file_location("ownership_fix_server", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_xref():
    services = types.ModuleType("services")
    identity = types.ModuleType("services.identity")
    identity.SCHEME_HOLDER = "holder_id"
    identity.SCHEME_ONDATE = "ondate_share_id"
    identity.SCHEME_SHARE = "share_id"
    utils = types.ModuleType("utils")
    text_mod = types.ModuleType("utils.text")
    text_mod.INDIVIDUAL_NAME = "شخص حقیقی"
    text_mod.normalize_fa = lambda value: str(value or "").replace("ي", "ی").replace("ك", "ک").strip()
    sys.modules.update({
        "services": services,
        "services.identity": identity,
        "utils": utils,
        "utils.text": text_mod,
    })
    spec = importlib.util.spec_from_file_location("ownership_fix_xref", ROOT / "services" / "xref.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percent_is_rebuilt_from_same_date_total():
    server = load_server()
    assert server.ownership_percent_for_date(25_000, 100_000) == 25.0
    assert server.ownership_percent_for_date(12_500, 50_000) == 25.0


def test_missing_or_invalid_denominator_is_not_guessed():
    server = load_server()
    assert server.ownership_percent_for_date(10, None) is None
    assert server.ownership_percent_for_date(10, 0) is None
    assert server.ownership_percent_for_date(10, -1) is None
    assert server.ownership_percent_for_date(None, 100) is None


def test_percent_is_not_capped_when_source_shares_exceed_total():
    server = load_server()
    # Preserve an impossible result for data-quality review; never hide it by capping.
    assert server.ownership_percent_for_date(102, 100) == 102.0


def test_masked_person_needs_two_exact_date_matches():
    xref = load_xref()
    observations = [
        dict(id_scheme="share_id", source_id=101, endpoint="shareholder_last",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="share_id", source_id=101, endpoint="shareholder_history",
             row_date="2026-01-02", shares=200, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-02", shares=200, name="شخص حقیقی"),
    ]
    rows, _ = xref.build_xref(observations, {("share_id", 101): "شخص حقیقی"})
    match = [r for r in rows if r["source_scheme"] == "ondate_share_id" and r["source_id"] == 501]
    assert len(match) == 1
    assert match[0]["status"] == xref.STATUS_INFERRED


def test_masked_person_single_date_match_is_not_linked():
    xref = load_xref()
    observations = [
        dict(id_scheme="share_id", source_id=101, endpoint="shareholder_last",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
    ]
    rows, _ = xref.build_xref(observations, {("share_id", 101): "شخص حقیقی"})
    match = [r for r in rows if r["source_scheme"] == "ondate_share_id" and r["source_id"] == 501]
    assert len(match) == 1
    assert match[0]["status"] == xref.STATUS_UNLINKED


def test_equal_shares_on_different_dates_do_not_create_identity_link():
    xref = load_xref()
    observations = [
        dict(id_scheme="share_id", source_id=101, endpoint="shareholder_last",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-02", shares=100, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-03", shares=100, name="شخص حقیقی"),
    ]
    rows, _ = xref.build_xref(observations, {("share_id", 101): "شخص حقیقی"})
    match = [r for r in rows if r["source_scheme"] == "ondate_share_id" and r["source_id"] == 501]
    assert len(match) == 1
    assert match[0]["status"] == xref.STATUS_UNLINKED


def test_frontend_does_not_group_holders_by_name():
    html = (ROOT / "ai_studio_code.html").read_text(encoding="utf-8")
    assert "const holderIdentityTokens" in html
    assert "byName" not in html
    assert "normalizeFaName(h.shareholder_name" not in html


def test_two_masked_people_with_equal_shares_remain_ambiguous():
    xref = load_xref()
    observations = [
        dict(id_scheme="share_id", source_id=101, endpoint="shareholder_last",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="share_id", source_id=102, endpoint="shareholder_last",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-01", shares=100, name="شخص حقیقی"),
        dict(id_scheme="ondate_share_id", source_id=501, endpoint="shareholders_on_date",
             row_date="2026-01-02", shares=100, name="شخص حقیقی"),
    ]
    rows, findings = xref.build_xref(
        observations,
        {("share_id", 101): "شخص حقیقی", ("share_id", 102): "شخص حقیقی"})
    match = [r for r in rows if r["source_scheme"] == "ondate_share_id" and r["source_id"] == 501]
    assert len(match) == 1
    assert match[0]["status"] == xref.STATUS_AMBIGUOUS
    assert any(f["category"] == "possible_duplicate_identity" for f in findings)


def test_latest_summary_sql_uses_exact_date_company_total():
    import sqlite3
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "LEFT JOIN company_daily cd ON cd.ins_code=h.ins_code AND cd.date=h.date" in server_source
    assert "100.0 * h.shares / cd.total_company_shares" in server_source
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE shareholder_holdings(
          ins_code TEXT, symbol TEXT, id_scheme TEXT, shareholder_id INTEGER,
          identity_status TEXT, date TEXT, jalali_date TEXT, shares INTEGER,
          ownership_percent_calc REAL, ownership_percent REAL, shares_change INTEGER,
          change_type TEXT, entry_status TEXT, price REAL, market_value REAL
        );
        CREATE TABLE company_daily(ins_code TEXT, date TEXT, total_company_shares INTEGER);
        CREATE TABLE site_excluded_holder_rows(
          ins_code TEXT, date TEXT, id_scheme TEXT, shareholder_id INTEGER
        );
        INSERT INTO shareholder_holdings VALUES
          ('X','X','share_id',1,'source_id','2025-01-01','1403-10-12',100,20,5,0,'unchanged','active',1,100),
          ('X','X','share_id',1,'source_id','2025-01-02','1403-10-13',100,20,5,0,'unchanged','active',1,100);
        INSERT INTO company_daily VALUES
          ('X','2025-01-01',1000), ('X','2025-01-02',2000);
    """)
    query = """
        SELECT h.ins_code, h.id_scheme, h.shareholder_id, MAX(h.date) AS latest_date,
          CASE WHEN cd.total_company_shares > 0 AND h.shares IS NOT NULL
               THEN 100.0 * h.shares / cd.total_company_shares ELSE NULL END
               AS ownership_percent_calc
        FROM shareholder_holdings h
        LEFT JOIN company_daily cd ON cd.ins_code=h.ins_code AND cd.date=h.date
        WHERE NOT EXISTS (
          SELECT 1 FROM site_excluded_holder_rows x
          WHERE x.ins_code=h.ins_code AND x.date=h.date
            AND x.id_scheme=h.id_scheme AND x.shareholder_id=h.shareholder_id)
        GROUP BY h.ins_code, h.id_scheme, h.shareholder_id
    """
    row = con.execute(query).fetchone()
    assert row["latest_date"] == "2025-01-02"
    assert row["ownership_percent_calc"] == 5.0
    con.close()


def test_empty_quantified_snapshot_is_not_reported_as_zero_percent():
    server = load_server()
    assert server.ownership_percent_for_date(0, 1000) == 0.0
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "if seen_identities else None" in source


def test_raw_percentage_is_never_a_fallback_for_corrected_totals():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '"COALESCE(w.deduped_ownership_percent_sum, "' not in source
    assert 'd["deduped_pct"] if d else None' in source
    assert 'w["deduped_ownership_percent_sum"],' in source
    html = (ROOT / "ai_studio_code.html").read_text(encoding="utf-8")
    assert "r.deduped_ownership_percent_sum ?? r.disclosed_ownership_percent_sum" not in html
    assert "s.deduped_ownership_percent_sum ?? s.disclosed_ownership_percent_sum" not in html
