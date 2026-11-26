"""Versioned HTTP API route dispatch for the legacy SiteApi adapter.

This module intentionally contains no HTTP server, database access, or business
logic. It maps the stable /api/v1 route contract to methods on the application
API object. Keep this function deterministic and side-effect free except for
the selected API method invocation.
"""

from .errors import ApiError


def dispatch_api(api, method, path, params, body):
    """Dispatch one already-parsed /api/v1 request to the matching API method.

    The caller is responsible for URL decoding, query parsing, body validation,
    HTTP response serialization, and converting ApiError into an HTTP response.
    """
    seg = [s for s in path.split("/") if s]
    # seg: ['api','v1', ...]
    rest = seg[2:]
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
