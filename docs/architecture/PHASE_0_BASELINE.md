# Phase 0 — Architecture Stabilization Baseline

**Baseline branch:** `main`  
**Baseline commit:** `ff062cf18fa4953ddd9bebeb9cbbb776741a8258`  
**Baseline date:** 2026-11-26  
**Scope:** repository-visible architecture only. No production database, live service, credentials, or runtime host was accessed.

## Executive summary

The public site is a browser-rendered React application published from `docs/`. Its local API is implemented by a single standard-library Python process in `server.py`. The API reads the TSETMC holdings database in SQLite read-only mode and maintains a separate SQLite sidecar for derived summaries and a small amount of mutable site state.

This is a **modular-monolith candidate, not a microservice system**. The first migration should preserve the public API and data semantics while creating internal module boundaries. Network service extraction must come after those boundaries and their contracts are tested.

## Observed components

| Component | Current location | Responsibility |
|---|---|---|
| Public frontend | `docs/index.html`, `docs/ui-refresh.css` | React/Babel UI, routes, charts and fetch calls |
| Legacy/local frontend assets | root HTML/CSS/JS assets | Local preview and design assets |
| HTTP server and routing | `server.py` | Static files, HTTP handling, API routing and error responses |
| Application/data logic | `SiteApi` and module-level functions in `server.py` | Market, stock, shareholder, ownership, portfolio, screener and admin queries |
| Sector-affinity calculation | `sector_affinity.py` | Stateful portfolio-to-sector aggregation |
| Source database | externally supplied `tsetmc_holdings.sqlite` | Pipeline-owned market and ownership records |
| Derived site database | `site_data.sqlite` beside `server.py` | Rebuildable materialized summaries plus legacy watchlist/filter state |
| Static data and samples | `docs/data/` | Public JSON and sample investor workbooks |
| Tests | `tests/` | Domain, ownership, portfolio, sector, export and Pages checks |
| CI | `.github/workflows/python-tests.yml` | Python 3.12 + pytest on pushes and pull requests |

## Runtime and API observations

- `server.py` uses `http.server.ThreadingHTTPServer` and SQLite from the Python standard library.
- The server opens the holdings database using SQLite `mode=ro` and `PRAGMA query_only=ON`.
- The sidecar uses WAL mode and is refreshed in-process by a daemon thread. A second daemon thread schedules refreshes when `--refresh-minutes` is positive.
- `SiteApi` owns database connections, refresh coordination, query methods and response construction.
- `Handler` serves static files, parses HTTP requests, applies CORS headers, dispatches routes and serializes JSON/CSV/XLSX responses.
- The frontend API base is selected in `docs/index.html`; outside a local port-8000 origin it currently falls back to `http://127.0.0.1:8000`. This does not point public GitHub Pages visitors to a central backend.
- The main-branch API dispatcher contains 27 method/path cases, including two explicit 501 placeholders for admin mutations.
- The current dispatcher accepts no authenticated-user context. The four `/api/v1/admin/*` read routes are not guarded in the main-branch dispatcher.
- `watchlist` and `saved_filters` have no user/tenant key in their sidecar schema; their current state is global to this site database.
- CORS in the main-branch handler allows `*`. This is not an authorization mechanism.

## Size and coupling signals

At the baseline commit, `server.py` is approximately 4,777 lines and combines HTTP transport, static file serving, API routing, date conversion, SQLite access, sidecar refresh, domain calculations, exports and admin queries. Its internal API methods frequently coordinate reads across the source database and sidecar. This makes extracting features directly into network services high-risk before their dependencies and data ownership are explicit.

`sector_affinity.py` is already a comparatively good extraction: it contains the sector-affinity accumulator independently from HTTP handling.

## SaaS branch inventory

At the baseline, SaaS work exists in separate, unmerged branches and draft PRs. Two implementation lines are visible:

1. A legacy `site_accounts.py` implementation integrated into a fork of `server.py`, using a separate SQLite account database.
2. A FastAPI `saas_core` implementation with PostgreSQL migrations and modules for auth, account, payments, wallet, support, analytics and administration.

The latter has a more explicit database boundary and is the preferred target direction, but it is not part of this baseline `main` commit. The two implementations must not be combined by copying files or merging unrelated branches. Select and integrate one identity/commercial implementation through a separately reviewed migration plan.

## Confirmed strengths to preserve

- The pipeline-owned market database is read-only to the site API.
- Derived data is materialized in a separate sidecar rather than written back to source records.
- Historical portfolio semantics have explicit domain tests, including missing disclosures, identity conflicts, duplicate ISIN handling and assumed-exit caveats.
- Sector-affinity logic is separated and tested.
- Static Pages validation and public-data privacy checks exist in the wider branch history; they should remain release gates when the newer SaaS chain is integrated.

## Risks and release blockers

| ID | Finding | Risk | Required treatment |
|---|---|---|---|
| R-01 | Browser fallback points public visitors to their own localhost | Public site cannot use a centrally hosted API | Configure an explicit HTTPS API origin or same-origin gateway before public launch |
| R-02 | Admin read endpoints are not authenticated in baseline `main` | Disclosure of operational metadata | Put server-side authentication and permission checks in place before exposing the API publicly |
| R-03 | Watchlist and saved filters are global sidecar rows | Cross-user state leakage and no account ownership | Move to user-scoped SaaS persistence with explicit ownership |
| R-04 | Mutable user state shares a SQLite file with rebuildable projections | Refresh/rebuild and backup lifecycle are coupled | Separate durable state from disposable projections |
| R-05 | One process handles HTTP, refresh, and CPU/DB-heavy queries | Contention and coupled failure/restart | Extract refresh worker and bound database/query concurrency |
| R-06 | Unmerged SaaS branches contain competing account implementations | Duplicate identity and billing authorities | Choose one canonical SaaS implementation and document migration ownership |
| R-07 | No explicit public API version/contract manifest exists | Accidental frontend breakage during extraction | Add and test the API contract before changing routing |
| R-08 | Wildcard CORS is enabled by the baseline handler | Cross-origin reads are too permissive for a future cookie-authenticated API | Replace with an explicit origin allow-list as part of the security rollout |

## Phase 0 exit criteria

- [x] Baseline commit and runtime components recorded.
- [x] Existing HTTP endpoint inventory written and machine-readable.
- [x] Data ownership and rebuildability documented.
- [x] Decisions recorded for modular-monolith-first migration and SaaS consolidation.
- [x] Route contract tests added against the existing dispatcher.
- [ ] CI must execute the new and existing suites successfully before Phase 1 is considered complete.

## Scope discipline

Phase 0 adds architecture evidence and contract tests only. It does not alter the API behavior, database schema, frontend URLs, authentication policy or deployment. Risks are recorded rather than silently changed during a structural refactor.
