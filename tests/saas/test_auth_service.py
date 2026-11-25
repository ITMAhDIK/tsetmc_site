import pytest
from saas_core.auth.service import hash_password,verify_password,normalize_phone,new_session_token,hash_session_token,new_otp

def test_password_hash_verifies_and_is_not_plaintext():
    encoded=hash_password("a-very-long-example-password")
    assert encoded!="a-very-long-example-password"
    assert verify_password(encoded,"a-very-long-example-password")
    assert not verify_password(encoded,"incorrect-password")

def test_password_policy_rejects_short_passwords():
    with pytest.raises(ValueError): hash_password("short")

def test_phone_requires_e164():
    assert normalize_phone("+989121234567")=="+989121234567"
    with pytest.raises(ValueError): normalize_phone("09121234567")

def test_session_token_is_random_and_only_hash_is_persisted():
    first,second=new_session_token(),new_session_token()
    assert first!=second
    assert hash_session_token(first)!=first
    assert len(hash_session_token(first))==64

def test_otp_is_six_digits():
    assert len(new_otp())==6 and new_otp().isdigit()
