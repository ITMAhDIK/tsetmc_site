# Cross-phase verification — Phases 0, 1, 2 and 3

Branch: `refactor/architecture-stabilization-phase0`

## Test strategy

The Phase 0 API manifest tests, Phase 1 dispatcher and real HTTP-handler tests,
Phase 2 worker/process-lock tests and Phase 3 MarketService tests run together
as one repository-wide pytest suite.

The Phase 3 fixture builds disposable SQLite source and sidecar files and
constructs the actual `SiteApi` composition root. It verifies the MarketService
dependency wiring, readiness gate, output fields, normalization and 404
behavior, cache behavior, sorting/filtering/paging, historical stock read model,
daily OHLCV fallback and dispatcher-to-service routing.

The Phase 0–2 integration tests continue to verify CLI-to-worker wiring, the
shared sidecar lock, failure propagation and HTTP route compatibility.

## Safety boundaries

No production database, sidecar, running server or deployment is accessed.
The source fixture is opened by the same read-only connection helper used by
the app. All generated data is confined to pytest temporary directories.

## Gate before Phase 4

Phase 4 must not start until the complete test suite is green on this branch.
