from cryptography.fernet import Fernet
import pytest

from commerce_service.api import validate_production_configuration


def _production_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "COMMERCE_DATABASE_URL",
        "postgresql://runtime:secret@commerce-db.example/tloomberg_commerce?sslmode=verify-full",
    )
    monkeypatch.setenv("COMMERCE_EXPECTED_DATABASE_NAME", "tloomberg_commerce")
    monkeypatch.setenv("COMMERCE_SERVICE_TOKEN", "commerce-private-token-" + "c" * 40)
    monkeypatch.setenv("IDENTITY_SERVICE_URL", "http://identity-api:8000")
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", "identity-private-token-" + "i" * 40)
    monkeypatch.setenv("COMMERCE_TRUSTED_HOSTS", "saas.example.com")
    monkeypatch.setenv("COMMERCE_ALLOWED_ORIGINS", "https://saas.example.com")
    monkeypatch.setenv("COMMERCE_DB_POOL_MIN", "1")
    monkeypatch.setenv("COMMERCE_DB_POOL_MAX", "8")
    monkeypatch.setenv("COMMERCE_DB_POOL_TIMEOUT_SECONDS", "5")


def test_production_config_accepts_separate_runtime_database_and_tokens(monkeypatch):
    _production_env(monkeypatch)
    # The web process must not receive migration-owner credentials.
    monkeypatch.delenv("COMMERCE_MIGRATION_DATABASE_URL", raising=False)
    validate_production_configuration()


def test_production_config_rejects_shared_identity_and_commerce_service_tokens(monkeypatch):
    _production_env(monkeypatch)
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", "commerce-private-token-" + "c" * 40)
    with pytest.raises(RuntimeError, match="must be different"):
        validate_production_configuration()


def test_production_config_rejects_unverified_database_tls(monkeypatch):
    _production_env(monkeypatch)
    monkeypatch.setenv(
        "COMMERCE_DATABASE_URL",
        "postgresql://runtime:secret@commerce-db.example/tloomberg_commerce",
    )
    with pytest.raises(RuntimeError, match="sslmode=verify-full"):
        validate_production_configuration()


def test_production_config_rejects_wildcard_host_and_http_origin(monkeypatch):
    _production_env(monkeypatch)
    monkeypatch.setenv("COMMERCE_TRUSTED_HOSTS", "*")
    with pytest.raises(RuntimeError, match="explicit hostnames"):
        validate_production_configuration()

    monkeypatch.setenv("COMMERCE_TRUSTED_HOSTS", "saas.example.com")
    monkeypatch.setenv("COMMERCE_ALLOWED_ORIGINS", "http://saas.example.com")
    with pytest.raises(RuntimeError, match="HTTPS origins"):
        validate_production_configuration()
