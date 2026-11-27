"""Private Commerce client used by SaaS for account subscription views and entitlements."""
from __future__ import annotations

import hmac
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID


class CommerceUnavailable(RuntimeError):
    """Commerce is unavailable or its private response cannot be trusted."""


def commerce_enabled() -> bool:
    return bool(os.getenv("COMMERCE_SERVICE_URL", "").strip())


def _request(path: str) -> dict:
    base = os.getenv("COMMERCE_SERVICE_URL", "").strip().rstrip("/")
    token = os.getenv("COMMERCE_SERVICE_TOKEN", "").strip()
    if not base or len(token) < 32:
        raise CommerceUnavailable("Commerce service is not configured")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.path not in {"", "/"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise CommerceUnavailable("Commerce service URL is invalid")
    request = Request(
        base + path,
        headers={"Accept": "application/json", "X-Commerce-Service-Token": token},
        method="GET",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except HTTPError:
        raise CommerceUnavailable("Commerce rejected a private service request") from None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        raise CommerceUnavailable("Commerce service is temporarily unavailable") from None
    if not isinstance(payload, dict):
        raise CommerceUnavailable("Commerce returned an invalid response")
    return payload


def subscription_history(user_id: UUID) -> list[dict]:
    payload = _request(f"/internal/users/{user_id}/subscriptions")
    subscriptions = payload.get("subscriptions")
    if not isinstance(subscriptions, list) or any(not isinstance(item, dict) for item in subscriptions):
        raise CommerceUnavailable("Commerce returned an invalid subscription history")
    return subscriptions


def get_entitlements(user_id: UUID) -> dict[str, bool]:
    payload = _request(f"/internal/users/{user_id}/entitlements")
    flags = payload.get("entitlements")
    expected = ("premium_analytics", "ownership_graphs", "investor_history")
    if not isinstance(flags, dict) or any(type(flags.get(key)) is not bool for key in expected):
        raise CommerceUnavailable("Commerce returned an invalid entitlement response")
    return {key: flags[key] for key in expected}
