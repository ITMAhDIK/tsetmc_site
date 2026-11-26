"""Security primitives for SaaS authentication."""
from __future__ import annotations
import hashlib, hmac, os, secrets
from datetime import datetime, timedelta, timezone
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
SESSION_TTL = timedelta(days=14)
OTP_TTL = timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5

class AuthenticationError(Exception):
    """Invalid credentials or expired/revoked challenges."""

class ConfigurationError(RuntimeError):
    """Missing required production secret."""

def _required_secret(name: str) -> bytes:
    value = os.environ.get(name, "")
    if len(value) < 32:
        raise ConfigurationError(f"{name} must contain at least 32 characters")
    return value.encode("utf-8")

def normalize_phone(phone: str) -> str:
    value = phone.strip()
    if (len(value) > 16 or not value.startswith("+") or
            not 8 <= len(value[1:]) <= 15 or value[1] not in "123456789" or
            any(char < "0" or char > "9" for char in value[1:])):
        raise ValueError("phone must be a valid ASCII E.164 number")
    return value

def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 256 or password.strip() != password:
        raise ValueError("password must be 12-256 characters without outer whitespace")
    return _PASSWORDS.hash(password)

def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORDS.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False

def new_session_token() -> str:
    return secrets.token_urlsafe(32)

def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def new_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"

def hash_otp(phone: str, purpose: str, otp: str) -> str:
    key = _required_secret("SAAS_OTP_HMAC_SECRET")
    return hmac.new(key, f"{phone}|{purpose}|{otp}".encode(), hashlib.sha256).hexdigest()

def verify_otp_hash(expected: str, phone: str, purpose: str, otp: str) -> bool:
    return hmac.compare_digest(expected, hash_otp(phone, purpose, otp))

def utc_now() -> datetime:
    return datetime.now(timezone.utc)
