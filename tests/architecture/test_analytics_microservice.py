from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_analytics_is_a_separate_production_compose_service():
    compose = read("docker-compose.saas.production.yml")
    for service in ("analytics-migrate:", "analytics-api:", "analytics-retention-worker:"):
        assert service in compose
    analytics_api = compose.split("  analytics-api:", 1)[1].split("\n  analytics-retention-worker:", 1)[0]
    assert "ANALYTICS_DATABASE_URL:" in analytics_api
    assert "IDENTITY_SERVICE_URL: http://identity-api:8000" in analytics_api
    assert "ANALYTICS_EXPECTED_DATABASE_NAME:" in analytics_api
    assert "SAAS_EXPECTED_DATABASE_NAME:" in analytics_api
    assert "IDENTITY_EXPECTED_DATABASE_NAME:" in analytics_api
    assert "COMMERCE_EXPECTED_DATABASE_NAME:" in analytics_api
    assert "read_only: true" in analytics_api
    assert "cap_drop:" in analytics_api and "- ALL" in analytics_api
    assert "expose:" in analytics_api and '"8000"' in analytics_api
    assert "ports:" not in analytics_api


def test_caddy_routes_versioned_analytics_api_to_its_own_upstream():
    caddy = read("Caddyfile.saas")
    assert "@analytics path /api/v1/analytics /api/v1/analytics/*" in caddy
    assert "uri strip_prefix /api/v1" in caddy
    assert "reverse_proxy analytics-api:8000" in caddy


def test_analytics_runtime_image_is_non_root_and_has_healthcheck():
    dockerfile = read("Dockerfile.analytics")
    assert "USER 10005:10005" in dockerfile
    assert "analytics_service.api:app" in dockerfile
    assert "COPY --chown=10005:10005 analytics_service /app/analytics_service" in dockerfile
    compose = read("docker-compose.saas.production.yml")
    assert "python -m analytics_service.retention_worker" in compose
    assert "HEALTHCHECK" in dockerfile


def test_analytics_schema_has_no_cross_database_identity_foreign_keys():
    schema = read("analytics_service/schema/001_analytics_core.sql")
    assert "CREATE TABLE IF NOT EXISTS analytics_events" in schema
    assert "CREATE TABLE IF NOT EXISTS analytics_consents" in schema
    assert "CREATE TABLE IF NOT EXISTS analytics_ingestion_limits" in schema
    assert "REFERENCES users" not in schema
    assert "REFERENCES sessions" not in schema


def test_saas_readiness_and_production_router_do_not_depend_on_analytics_tables():
    source = read("saas_core/auth/api.py")
    required_tables = source.split("_REQUIRED_SCHEMA_TABLES = (", 1)[1].split(")", 1)[0]
    required_columns = source.split("_REQUIRED_SCHEMA_COLUMNS = (", 1)[1].split(")", 1)[0]
    assert "analytics_events" not in required_tables
    assert "analytics_consents" not in required_tables
    assert "analytics_events.event_order" not in required_columns
    assert 'if APP_ENV not in {"prod", "production"}:' in source



def test_browser_sdk_is_loaded_but_remains_disabled_without_an_api_origin():
    index = read("docs/index.html")
    config = read("docs/analytics-config.js")
    assert '<script src="./analytics-config.js"></script>' in index
    assert '<script src="./analytics.js"></script>' in index
    assert 'href="./analytics.css"' in index
    assert 'apiBase: ""' in config
    assert "TLOOMBERG_ANALYTICS_ROUTE_SYNCED" in index
