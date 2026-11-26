from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INITIAL = (ROOT / "saas_core" / "schema" / "001_initial.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "saas_core" / "schema" / "002_product_features_referrals_analytics.sql").read_text(encoding="utf-8")
README = (ROOT / "saas_core" / "README.md").read_text(encoding="utf-8")


def test_account_database_has_independent_identity_and_role_models():
    for table in ("users", "auth_identities", "sessions", "roles", "permissions", "user_roles"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in INITIAL
    assert "dedicated PostgreSQL database" in README


def test_requested_subscription_prices_and_payment_methods_exist():
    assert "premium_monthly','اشتراک ویژه یک‌ماهه',30,899000" in INITIAL
    assert "premium_quarterly','اشتراک ویژه سه‌ماهه',90,2499000" in INITIAL
    assert "provider IN ('snappay','digipay')" in INITIAL
    assert "phone_e164 VARCHAR(20) NOT NULL" in INITIAL


def test_wallet_does_not_allow_withdrawing_referral_gifts():
    assert "bucket IN ('cash','gift')" in INITIAL
    assert "available_to_withdraw_toman" in INITIAL
    assert "WHERE status IN ('requested','approved')" in INITIAL
    assert "gift first, then cash" in (ROOT / "saas_core" / "domain" / "wallet.py").read_text(encoding="utf-8")


def test_referral_and_support_records_are_durable_and_idempotent():
    for table in ("referral_codes", "referrals", "referral_rewards", "tickets", "ticket_messages", "faq_articles"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in INITIAL
    assert "CREATE TABLE IF NOT EXISTS referral_invitations" in MIGRATION
    assert "UNIQUE(invited_phone_e164)" in MIGRATION
    assert "DEFAULT 100000" in INITIAL
    assert "notification_outbox" in INITIAL


def test_analytics_records_components_scroll_and_active_dwell_time():
    assert "CREATE TABLE IF NOT EXISTS analytics_consents" in INITIAL
    assert "CREATE TABLE IF NOT EXISTS analytics_events" in INITIAL
    for column in ("component_id", "scroll_depth_pct", "active_duration_ms", "anonymous_id", "session_id"):
        assert column in INITIAL
    assert "CREATE TABLE IF NOT EXISTS analytics_consent_history" in MIGRATION
    assert "idx_analytics_events_path_component_time" in MIGRATION


def test_free_plan_cannot_access_graphs_and_paid_plans_receive_customer_features():
    assert "f.code IN ('market.overview','stock.basic')" in MIGRATION
    assert "p.code IN ('premium_monthly','premium_quarterly')" in MIGRATION
    assert "'shareholders.graphs'" in MIGRATION
    assert "Market Data Manager" in MIGRATION
    assert "market.admin" in MIGRATION
