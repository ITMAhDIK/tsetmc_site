from commerce_service.payments.domain import PaymentNotConfigured
from commerce_service.payments.gateways import get_gateway
from scripts.manage_commerce_migrations import (
    MigrationError, _validate_history, load_migrations, validate_connection_tls,
)


def test_commerce_migrations_are_ordered_and_unique():
    migrations = load_migrations()
    versions = [item[0] for item in migrations]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == "001_commerce_core"


def test_commerce_migration_history_rejects_checksum_drift_and_unknown_versions():
    items = [
        ("001_commerce_core", "a" * 64, "SELECT 1;"),
        ("002_add_feature", "b" * 64, "SELECT 2;"),
    ]
    assert _validate_history(items, {"001_commerce_core": "a" * 64}) == [items[1]]
    try:
        _validate_history(items, {"001_commerce_core": "c" * 64})
    except MigrationError as exc:
        assert "changed after application" in str(exc)
    else:
        raise AssertionError("checksum drift must be rejected")
    try:
        _validate_history(items, {"099_unknown": "d" * 64})
    except MigrationError as exc:
        assert "unknown migration versions" in str(exc)
    else:
        raise AssertionError("unknown applied versions must be rejected")


def test_commerce_remote_migrations_require_hostname_verified_tls():
    validate_connection_tls("postgresql://local@localhost:5432/tloomberg_commerce")
    try:
        validate_connection_tls("postgresql://migrator@db.example:5432/tloomberg_commerce")
    except MigrationError as exc:
        assert "sslmode=verify-full" in str(exc)
    else:
        raise AssertionError("remote connection without verify-full must be rejected")
    validate_connection_tls(
        "postgresql://migrator@db.example:5432/tloomberg_commerce?sslmode=verify-full"
    )


def test_payment_provider_adapters_remain_disabled_until_official_contract():
    try:
        get_gateway("digipay").initiate(None)
    except PaymentNotConfigured:
        pass
    else:
        raise AssertionError("provider adapter must fail closed without merchant contract")
