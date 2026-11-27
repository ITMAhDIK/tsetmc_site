"""HTTP adapter to the independently deployed Market Data service."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .errors import ApiError


def proxy_market_data_request(path: str, params: dict):
    """Forward a read-only dashboard route to Market Data when configured.

    If the dependency is configured but unavailable, fail closed with 503.
    The modular-monolith fallback is used only when no service URL is set.
    """
    base = os.getenv("MARKET_DATA_SERVICE_URL", "").strip().rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.path not in {"", "/"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ApiError(503, "سرویس اطلاعات بازار پیکربندی معتبری ندارد.")

    pairs = []
    for key, values in (params or {}).items():
        if isinstance(values, (list, tuple)):
            pairs.extend((key, value) for value in values)
        else:
            pairs.append((key, values))
    query = urlencode(pairs, doseq=True)
    safe_path = quote(path, safe="/")
    url = base + safe_path + (("?" + query) if query else "")
    request = Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            raw = response.read(2 * 1024 * 1024)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Market Data response must be a JSON object")
            return payload, response.status, "json"
    except HTTPError as exc:
        detail = "خطا در دریافت اطلاعات بازار."
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                detail = payload["detail"]
        except Exception:
            pass
        raise ApiError(exc.code if 400 <= exc.code <= 599 else 502, detail) from None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ApiError(503, "سرویس اطلاعات بازار موقتاً در دسترس نیست.") from None
