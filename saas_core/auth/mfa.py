"""RFC 6238 TOTP primitives for privileged SaaS accounts.

Secrets are encrypted by callers before persistence. This module has no database
or HTTP dependencies, making the time-window and replay policy directly testable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_totp_secret() -> str:
    """Return a 160-bit Base32 secret without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not 16 <= len(secret) <= 64:
        raise ValueError("TOTP secret is invalid")
    normalized = secret.strip().upper()
    if not all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in normalized):
        raise ValueError("TOTP secret must be Base32")
    return base64.b32decode(normalized + "=" * ((8 - len(normalized) % 8) % 8))


def totp_code(secret: str, *, for_time: float | None = None, step: int = 30) -> str:
    """Generate a six-digit SHA-1 TOTP value using a 30-second time step."""
    if step < 15 or step > 120:
        raise ValueError("TOTP time step is outside the allowed range")
    moment = time.time() if for_time is None else for_time
    if moment < 0:
        raise ValueError("TOTP time cannot be negative")
    counter = int(moment // step)
    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def verify_totp(secret: str, supplied: str, *, at_time: float | None = None,
                window: int = 1, step: int = 30) -> bool:
    """Constant-time verification with a bounded clock-drift window."""
    if not isinstance(supplied, str) or len(supplied) != 6 or not supplied.isascii() or not supplied.isdigit():
        return False
    if isinstance(window, bool) or not isinstance(window, int) or not 0 <= window <= 2:
        raise ValueError("TOTP verification window must be between zero and two steps")
    moment = time.time() if at_time is None else at_time
    try:
        for delta in range(-window, window + 1):
            candidate = totp_code(secret, for_time=moment + delta * step, step=step)
            if hmac.compare_digest(candidate, supplied):
                return True
    except (TypeError, ValueError):
        return False
    return False


def provisioning_uri(secret: str, *, account: str, issuer: str = "Tloomberg",
                     period: int = 30, digits: int = 6) -> str:
    if not account or not issuer or period != 30 or digits != 6:
        raise ValueError("TOTP provisioning parameters are invalid")
    _decode_secret(secret)
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={quote(secret)}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )
