import pytest

from saas_core.auth.mfa import (
    matching_counter,
    new_totp_secret,
    provisioning_uri,
    totp_code,
    verify_totp,
)


def test_rfc6238_sha1_vector_uses_six_digit_code():
    # RFC 6238 SHA-1 test secret; the six-digit truncation of 94287082.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret, for_time=59) == "287082"
    assert verify_totp(secret, "287082", at_time=59, window=0)


def test_totp_accepts_bounded_clock_drift_and_returns_counter():
    secret = new_totp_secret()
    now = 1_700_000_000
    previous_step_code = totp_code(secret, for_time=now - 30)
    assert verify_totp(secret, previous_step_code, at_time=now, window=1)
    assert matching_counter(secret, previous_step_code, at_time=now, window=1) == (now // 30) - 1
    assert not verify_totp(secret, previous_step_code, at_time=now, window=0)


def test_totp_rejects_malformed_codes_secrets_and_unbounded_windows():
    secret = new_totp_secret()
    for invalid in ("12345", "1234567", "１２３４５６", "12a456", ""):
        assert not verify_totp(secret, invalid, at_time=1_700_000_000)
    with pytest.raises(ValueError):
        totp_code("not-base32-secret", for_time=59)
    with pytest.raises(ValueError):
        verify_totp(secret, "123456", window=99)


def test_new_secret_is_unique_and_has_expected_entropy_encoding():
    first = new_totp_secret()
    second = new_totp_secret()
    assert first != second
    assert len(first) == 32
    assert "=" not in first


def test_provisioning_uri_is_compatible_with_standard_authenticator_apps():
    secret = new_totp_secret()
    uri = provisioning_uri(secret, account="+989121234567")
    assert uri.startswith("otpauth://totp/Tloomberg%3A%2B989121234567?")
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_provisioning_uri_rejects_empty_identity_and_unexpected_parameters():
    secret = new_totp_secret()
    with pytest.raises(ValueError):
        provisioning_uri(secret, account="")
    with pytest.raises(ValueError):
        provisioning_uri(secret, account="admin", period=60)
