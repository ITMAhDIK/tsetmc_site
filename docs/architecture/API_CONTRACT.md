# Current HTTP API Contract (baseline main)

This document describes the route behavior that exists in baseline commit
`ff062cf18fa4953ddd9bebeb9cbbb776741a8258`. It is a compatibility contract for
the gradual extraction. It is **not** an endorsement that every route is safe
to expose publicly.

Machine-readable counterpart: [api-contract.json](api-contract.json).
The pytest contract suite executes each manifest route against the existing
dispatcher using a recording fake API, so path/method/handler/response changes
must be deliberate.

## Contract conventions

- API prefix: `/api/v1`.
- Query parameters are passed to handlers as the `parse_qs` mapping (values
  are lists of strings).
- JSON request bodies are passed to handlers as dictionaries.
- Standard JSON routes return `(payload, 200, "json")`.
- Screener export returns `(bytes, 200, "xlsx")`.
- Unknown method/path combinations raise `ApiError(404, ...)`.
- Two admin mutation routes are intentionally unimplemented and return 501.
- The current main-branch dispatcher does not receive a user/session argument.
  All routes below are unauthenticated at this layer. Admin routes are a
  security blocker until protected server-side.

## Route inventory

| Method | Path | Handler | Response | Current access |
|---|---|---|---|---|
| GET | `/market/overview` | market_overview | JSON | Unauthenticated |
| GET | `/market/watch` | market_watch | JSON | Unauthenticated |
| GET | `/stocks/search` | stocks_search | JSON | Unauthenticated |
| GET | `/stocks/{symbol}` | stock_detail | JSON | Unauthenticated |
| GET | `/stocks/{symbol}/shareholders` | stock_shareholders | JSON | Unauthenticated |
| GET | `/stocks/{symbol}/ownership-series` | stock_ownership_series | JSON | Unauthenticated |
| GET | `/stocks/{symbol}/corporate-actions` | stock_corporate_actions | JSON | Unauthenticated |
| GET | `/stocks/{symbol}/data-quality` | stock_data_quality | JSON | Unauthenticated |
| GET | `/stocks/{symbol}/prices` | stock_prices | JSON | Unauthenticated |
| GET | `/shareholders/search` | shareholders_search | JSON | Unauthenticated |
| GET | `/shareholders/observation-history` | shareholder_observation_history | JSON | Unauthenticated |
| GET | `/portfolios/investors` | portfolio_investors | JSON | Unauthenticated |
| GET | `/portfolios/{id}/history` | portfolio_history | JSON | Unauthenticated |
| GET | `/portfolios/{id}` | portfolio_detail | JSON | Unauthenticated |
| POST | `/screener/query` | screener_query | JSON | Unauthenticated |
| POST | `/screener/export` | screener_export | XLSX | Unauthenticated |
| GET | `/screener/saved-filters` | saved_filters_list | JSON | Unauthenticated |
| POST | `/screener/saved-filters` | saved_filters_create | JSON | Unauthenticated |
| GET | `/watchlists/default` | watchlist_get | JSON | Unauthenticated |
| POST | `/watchlists/default/symbols` | watchlist_add | JSON | Unauthenticated |
| DELETE | `/watchlists/default/symbols/{symbol}` | watchlist_remove | JSON | Unauthenticated |
| GET | `/admin/overview` | admin_overview | JSON | Unauthenticated — blocker |
| GET | `/admin/manifests` | admin_manifests | JSON | Unauthenticated — blocker |
| GET | `/admin/errors` | admin_errors | JSON | Unauthenticated — blocker |
| GET | `/admin/identity/queue` | admin_identity_queue | JSON | Unauthenticated — blocker |
| POST | `/admin/ingestion/trigger` | — | 501 JSON error | Not implemented |
| POST | `/admin/identity/resolve` | — | 501 JSON error | Not implemented |

## Compatibility and extraction rules

1. Preserve method, path, parameter/body forwarding and response content type
   while moving dispatch code.
2. Do not change JSON field names, units, missing-data semantics or sort/paging
   behavior as part of the route extraction.
3. Do not turn 501 placeholders into live mutations as a side effect of
   refactoring.
4. Any future authentication or authorization change must have explicit
   endpoint-level tests and a documented rollout; do not infer permissions
   from the UI.
5. Keep a compatibility adapter at the old route boundary until the frontend
   and any external consumers have migrated.
6. Add new routes by updating the JSON manifest and contract tests in the
   same change.
