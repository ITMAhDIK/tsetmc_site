"""HTTP adapter for the independently deployed stock Screener service."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .errors import ApiError

_JSON = "application/json"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ALLOWED_PATHS = {
    "/api/v1/screener/query",
    "/api/v1/screener/export",
}
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def proxy_screener_request(path: str, body: dict):
    """Forward a screener POST when configured; fail closed on service outages.

    Returns the existing dispatcher tuple: (payload, status, content_type),
    where content_type is "json" or "xlsx".
    """
    if path not in _ALLOWED_PATHS:
        raise ApiError(404, "مسیر سرویس غربال‌گری معتبر نیست.")
    base = os.getenv("SCREENER_SERVICE_URL", "").strip().rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ApiError(503, "سرویس غربال‌گری پیکربندی معتبری ندارد.")
    if not isinstance(body, dict):
        raise ApiError(400, "بدنه‌ی درخواست غربال‌گری نامعتبر است.")

    try:
        timeout = float(os.getenv("SCREENER_HTTP_TIMEOUT_SECONDS", "20"))
        if timeout <= 0 or timeout > 120:
            raise ValueError("Screener HTTP timeout must be between 0 and 120 seconds")
        payload = json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ValueError("Screener request exceeded the 1 MiB limit")
    except (TypeError, ValueError, OverflowError):
        raise ApiError(400, "بدنه‌ی درخواست غربال‌گری معتبر نیست.") from None

    request = Request(
        base + path,
        data=payload,
        headers={
            "Accept": f"{_JSON}, {_XLSX}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("Screener response exceeded the 16 MiB limit")
            response_type = response.headers.get("Content-Type", "")
            response_type = response_type.split(";", 1)[0].strip().lower()
            if response_type == _XLSX:
                if path != "/api/v1/screener/export":
                    raise ValueError("Unexpected XLSX response from screener query")
                return raw, response.status, "xlsx"
            if response_type == _JSON:
                if path != "/api/v1/screener/query":
                    raise ValueError("Unexpected JSON response from screener export")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Screener response must be a JSON object")
                return result, response.status, "json"
            raise ValueError("Unsupported Screener response content type")
    except HTTPError as exc:
        detail = "خطا در اجرای غربال‌گری."
        try:
            error_payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
            if isinstance(error_payload, dict) and isinstance(error_payload.get("detail"), str):
                detail = error_payload["detail"]
        except Exception:
            pass
        raise ApiError(exc.code if 400 <= exc.code <= 599 else 502, detail) from None
    except (
        URLError, TimeoutError, OSError, UnicodeError,
        json.JSONDecodeError, ValueError,
    ):
        raise ApiError(503, "سرویس غربال‌گری موقتاً در دسترس نیست.") from None
