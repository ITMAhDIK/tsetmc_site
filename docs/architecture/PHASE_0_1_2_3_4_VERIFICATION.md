# Cross-phase verification — Phases 0, 1, 2, 3 and 4

Branch: `refactor/architecture-stabilization-phase0`

## Verification scope

The complete repository test suite runs all existing Phase 0 API-contract,
Phase 1 HTTP/dispatcher, Phase 2 external refresh worker/locking and Phase 3
MarketService tests together with the new Phase 4 OwnershipService tests.

Phase 4 tests construct the actual `SiteApi` composition root and use disposable
source and Sidecar SQLite files. They cover shareholder snapshots and same-date
identity resolution, ownership time series, event valuation fallback to
`trading_days`, cross-company identity grouping, shareholder observation history,
readiness validation, source read-only behavior and versioned dispatcher routing.

The pre-existing representative tie-break tests also remain in the full suite.
They continue to call the legacy `SiteApi._resolve_date_rows` compatibility
adapter, verifying that old internal call sites retain their behavior.

## Test isolation

No production holdings database, production Sidecar, network endpoint or running
server is used or modified. All test data exists only under pytest temporary
directories. The source connection is opened by the production read-only
connection helper.

## Gate before Phase 5

Phase 5 must not start until the complete suite passes on both Push and Pull
Request CI runs for the same branch head.
