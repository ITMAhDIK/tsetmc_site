"""Versioned HTTP API route dispatch for the legacy SiteApi adapter.

This module intentionally contains no HTTP server, database access, or business
logic. It maps the stable /api/v1 route contract to methods on the application
API object. Keep this function deterministic and side-effect free except for
the selected API method invocation.
"""

import os

from .errors import ApiError
from .market_data_client import proxy_market_data_request


def _public_read_only() -> bool:
    return os.getenv("MARKET_PUBLIC_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def _allow_public_read(method: str, rest: list[str]) -> bool:
    """Allow only public market reads and side-effect-free screener POST queries."""
    if method == "POST" and rest in (["screener", "query"], ["screener", "export"]):
        return True
    if method != "GET":
        return False
    if rest in (["market", "overview"], ["market", "watch"],
                ["market", "instruments"], ["stocks", "search"], ["shareholders", "search"],
                ["shareholders", "observation-history"], ["portfolios", "investors"]):
        return True
    if len(rest) == 3 and rest[:2] == ["market", "instruments"]:
        return True
    if len(rest) == 2 and rest[0] in {"stocks", "portfolios"}:
        return True
    if len(rest) == 3 and rest[0] == "stocks" and rest[2] in {
        "shareholders", "ownership-series", "corporate-actions", "data-quality", "prices"
    }:
        return True
    if len(rest) == 3 and rest[0] == "portfolios" and rest[2] == "history":
        return True
    return False


def dispatch_api(api, method, path, params, body):
    """Dispatch one already-parsed /api/v1 request to the matching API method.

    The caller is responsible for URL decoding, query parsing, body validation,
    HTTP response serialization, and converting ApiError into an HTTP response.
    """
    seg = [s for s in path.split("/") if s]
    # seg: ['api','v1', ...]
    rest = seg[2:]
    # Production market deployments are public read-only APIs. Sidecar
    # write endpoints and operational admin routes must never be reachable
    # through the unauthenticated public server.
    if _public_read_only() and not _allow_public_read(method, rest):
        if rest and rest[0] == "admin":
            raise ApiError(404, "مسیر API یافت نشد: %s %s" % (method, path))
        raise ApiError(403, "درگاه عمومی فقط دسترسی خواندنی به داده‌های بازار دارد.")

    # Compatibility gateway: deployments may route these requests through
    # Caddy directly, while local/API-gateway deployments can use this client.
    is_market_data_route = (
        method == "GET"
        and (
            rest in (["stocks", "search"], ["market", "instruments"])
            or (len(rest) == 3 and rest[:2] == ["market", "instruments"])
            or (len(rest) == 3 and rest[0] == "stocks" and rest[2] == "prices")
        )
    )
    if is_market_data_route:
        proxied = proxy_market_data_request(path, params)
        if proxied is not None:
            return proxied
    if method == "GET" and rest == ["market", "overview"]:
        return api.market_overview(), 200, "json"
    if method == "GET" and rest == ["market", "watch"]:
        return api.market_watch(params), 200, "json"
    if method == "GET" and rest == ["stocks", "search"]:
        return api.stocks_search(params), 200, "json"
    if method == "GET" and len(rest) == 2 and rest[0] == "stocks":
        return api.stock_detail(rest[1]), 200, "json"
    if method == "GET" and len(rest) == 3 and rest[0] == "stocks":
        if rest[2] == "shareholders":
            return api.stock_shareholders(rest[1], params), 200, "json"
        if rest[2] == "ownership-series":
            return api.stock_ownership_series(rest[1], params), 200, "json"
        if rest[2] == "corporate-actions":
            return api.stock_corporate_actions(rest[1]), 200, "json"
        if rest[2] == "data-quality":
            return api.stock_data_quality(rest[1]), 200, "json"
        if rest[2] == "prices":
            return api.stock_prices(rest[1]), 200, "json"
    if method == "GET" and rest == ["shareholders", "search"]:
        return api.shareholders_search(params), 200, "json"
    if method == "GET" and rest == ["shareholders", "observation-history"]:
        return api.shareholder_observation_history(params), 200, "json"
    if method == "GET" and rest == ["portfolios", "investors"]:
        return api.portfolio_investors(params), 200, "json"
    if method == "GET" and len(rest) == 3 and rest[0] == "portfolios" and rest[2] == "history":
        return api.portfolio_history(rest[1], params), 200, "json"
    if method == "GET" and len(rest) == 2 and rest[0] == "portfolios":
        return api.portfolio_detail(rest[1], params), 200, "json"
    if method == "POST" and rest == ["screener", "query"]:
        return api.screener_query(body), 200, "json"
    if method == "POST" and rest == ["screener", "export"]:
        return api.screener_export(body), 200, "xlsx"
    if method == "GET" and rest == ["screener", "saved-filters"]:
        return api.saved_filters_list(), 200, "json"
    if method == "POST" and rest == ["screener", "saved-filters"]:
        return api.saved_filters_create(body), 200, "json"
    if method == "GET" and rest == ["watchlists", "default"]:
        return api.watchlist_get(), 200, "json"
    if method == "POST" and rest == ["watchlists", "default", "symbols"]:
        return api.watchlist_add(body), 200, "json"
    if method == "DELETE" and len(rest) == 4 and rest[:3] == [
            "watchlists", "default", "symbols"]:
        return api.watchlist_remove(rest[3]), 200, "json"
    if method == "GET" and rest == ["admin", "overview"]:
        return api.admin_overview(), 200, "json"
    if method == "GET" and rest == ["admin", "manifests"]:
        return api.admin_manifests(params), 200, "json"
    if method == "GET" and rest == ["admin", "errors"]:
        return api.admin_errors(params), 200, "json"
    if method == "GET" and rest == ["admin", "identity", "queue"]:
        return api.admin_identity_queue(params), 200, "json"
    if method == "POST" and rest == ["admin", "ingestion", "trigger"]:
        raise ApiError(
            501, "اجرای ایمپورت از پنل مدیریت وصل نشده است. "
                 "پایپلاین جمع‌آوری فقط با دستور مستقیم و با تأیید جداگانه اجرا می‌شود.")
    if method == "POST" and rest == ["admin", "identity", "resolve"]:
        raise ApiError(
            501, "تأیید هویت از پنل مدیریت وصل نشده است؛ "
                 "در حال حاضر هیچ لینک هویتی در انتظار تأیید نیست.")
    raise ApiError(404, "مسیر API یافت نشد: %s %s" % (method, path))
