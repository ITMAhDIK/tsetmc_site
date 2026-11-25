import pytest

from saas_core.auth.service import (
    ConfigurationError,
    hash_otp,
    hash_password,
    hash_session_token,
    new_session_token,
    normalize_phone,
    verify_otp_hash,
    verify_password,
)


def test_password_hash_is_argon2id_and_verifies_without_storing_plaintext():
    password = "a-long-test-password-2026"
    password_hash = hash_password(password)

    assert password_hash.startswith("$argon2id$")
    assert password_hash != password
    assert verify_password(password_hash, password) is True
    assert verify_password(password_hash, "a-different-password") is False


@pytest.mark.parametrize(
    "password",
    [
        "short",
        " leading space password",
        "trailing space password ",
        "x" * 257,
    ],
)
def test_password_policy_rejects_weak_or_ambiguous_values(password):
    with pytest.raises(ValueError):
        hash_password(password)


def test_password_verification_fails_closed_for_malformed_hash():
    assert verify_password("not-an-argon2-hash", "some-password") is False


def test_session_tokens_are_random_and_only_the_digest_is_stored():
    first = new_session_token()
    second = new_session_token()

    assert first != second
    assert len(first) >= 32
    assert hash_session_token(first) == hash_session_token(first)
    assert hash_session_token(first) != first
    assert len(hash_session_token(first)) == 64


def test_otp_hmac_is_secret_backed_and_domain_separated(monkeypatch):
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "s" * 48)

    expected = hash_otp("+989121234567", "login", "123456")

    assert verify_otp_hash(expected, "+989121234567", "login", "123456") is True
    assert verify_otp_hash(expected, "+989121234567", "signup", "123456") is False
    assert verify_otp_hash(expected, "+989121234567", "login", "654321") is False


def test_otp_hmac_fails_closed_when_secret_is_missing_or_too_short(monkeypatch):
    monkeypatch.delenv("SAAS_OTP_HMAC_SECRET", raising=False)
    with pytest.raises(ConfigurationError):
        hash_otp("+989121234567", "login", "123456")

    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "too-short")
    with pytest.raises(ConfigurationError):
        hash_otp("+989121234567", "login", "123456")


def test_phone_normalization_accepts_e164_and_rejects_invalid_values():
    assert normalize_phone("+989121234567") == "+989121234567"

    for invalid in ("09121234567", "+01234567", "+123", "+1234567890123456"):
        with pytest.raises(ValueError):
            normalize_phone(invalid)


def test_otp_verification_rejects_wrong_digest(monkeypatch):
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "s" * 48)
    assert verify_otp_hash("0" * 64, "+989121234567", "login", "123456") is False


def test_migration_history_rejects_checksum_drift_and_unknown_versions():
    from scripts.manage_saas_migrations import MigrationError, _validate_history

    migrations = [
        ("001_initial", "a" * 64, "SELECT 1;"),
        ("002_payments", "b" * 64, "SELECT 2;"),
    ]

    assert _validate_history(migrations, {"001_initial": "a" * 64}) == [
        ("002_payments", "b" * 64, "SELECT 2;")
    ]

    with pytest.raises(MigrationError, match="changed after application"):
        _validate_history(migrations, {"001_initial": "c" * 64})

    with pytest.raises(MigrationError, match="unknown migration versions"):
        _validate_history(migrations, {"099_unknown": "d" * 64})


def test_migration_role_name_allow_list_rejects_sql_fragments():
    from scripts.manage_saas_migrations import MigrationError, ROLE_NAME

    assert ROLE_NAME.fullmatch("saas_runtime") is not None
    for value in ('saas_runtime; DROP ROLE postgres', "Runtime", "x" * 64, ""):
        assert ROLE_NAME.fullmatch(value) is None


def test_production_configuration_requires_explicit_https_hosts_and_separate_secrets(monkeypatch):
    from cryptography.fernet import Fernet
    import saas_core.auth.api as auth_api

    monkeypatch.setattr(auth_api, "APP_ENV", "production")
    monkeypatch.setattr(auth_api, "COOKIE_SECURE", True)
    monkeypatch.setattr(auth_api, "TRUSTED_HOSTS", ["api.example.com"])
    monkeypatch.setattr(auth_api, "ALLOWED_ORIGINS", ["https://app.example.com"])
    monkeypatch.setenv("SAAS_DATABASE_URL", "postgresql://runtime:secret@db.example/tloomberg_saas")
    monkeypatch.setenv("SAAS_EXPECTED_DATABASE_NAME", "tloomberg_saas")
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "otp-secret-" + "x" * 40)
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("SAAS_DB_POOL_MIN", "1")
    monkeypatch.setenv("SAAS_DB_POOL_MAX", "8")
    monkeypatch.setenv("SAAS_DB_POOL_TIMEOUT_SECONDS", "4")

    auth_api.validate_production_configuration()


def test_production_configuration_rejects_wildcard_hosts_and_shared_secrets(monkeypatch):
    from cryptography.fernet import Fernet
    import saas_core.auth.api as auth_api

    monkeypatch.setattr(auth_api, "APP_ENV", "production")
    monkeypatch.setattr(auth_api, "COOKIE_SECURE", True)
    monkeypatch.setattr(auth_api, "TRUSTED_HOSTS", ["*"])
    monkeypatch.setattr(auth_api, "ALLOWED_ORIGINS", ["https://app.example.com"])
    monkeypatch.setenv("SAAS_DATABASE_URL", "postgresql://runtime:secret@db.example/tloomberg_saas")
    monkeypatch.setenv("SAAS_EXPECTED_DATABASE_NAME", "tloomberg_saas")
    shared_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", shared_key)
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", shared_key)

    with pytest.raises(ConfigurationError, match="OTP and outbox encryption secrets"):
        auth_api.validate_production_configuration()

    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(ConfigurationError, match="explicit production hostnames"):
        auth_api.validate_production_configuration()


def test_database_pool_configuration_rejects_unbounded_values(monkeypatch):
    import saas_core.auth.api as auth_api

    monkeypatch.setenv("SAAS_DB_POOL_MIN", "0")
    monkeypatch.setenv("SAAS_DB_POOL_MAX", "10")
    with pytest.raises(ConfigurationError, match="1 <= min <= max"):
        auth_api._database_pool_config()

    monkeypatch.setenv("SAAS_DB_POOL_MIN", "2")
    monkeypatch.setenv("SAAS_DB_POOL_MAX", "1")
    with pytest.raises(ConfigurationError, match="1 <= min <= max"):
        auth_api._database_pool_config()

    monkeypatch.setenv("SAAS_DB_POOL_MIN", "1")
    monkeypatch.setenv("SAAS_DB_POOL_MAX", "101")
    with pytest.raises(ConfigurationError, match="1 <= min <= max"):
        auth_api._database_pool_config()
