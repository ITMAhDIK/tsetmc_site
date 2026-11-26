# Cross-phase verification — Phases 0, 1 and 2

Branch: `refactor/architecture-stabilization-phase0`

## Why this suite was added

The earlier suites validated the API manifest, HTTP adapter and refresh worker mostly
as separate units. This additional suite checks the integration seams between the
server CLI, the external refresh runtime, `SiteApi.refresh()`, the shared sidecar
path and the worker's exit status.

## Integration checks

- The server CLI accepts `--refresh-mode external`, does not silently change the
  selected sidecar path, and passes the mode and interval to the runtime selector.
- The worker CLI constructs the API using the same source DB and sidecar paths and
  reaches the real `SiteApi.refresh()` method.
- The worker and API use the same per-sidecar `.refresh.lock`; when another process
  owns that lock, refresh is skipped and the worker exits non-zero.
- A projection failure recorded by `SiteApi` is propagated as worker exit code 1.
- Existing Phase 0 API manifest and Phase 1 HTTP tests remain part of the same
  repository-wide pytest run.

## Test isolation and limits

All integration cases use temporary directories and disposable placeholder files.
They monkeypatch only the expensive projection builder and HTTP listener. No real
holdings database is opened, no production sidecar is touched, and no network
request or deployment is performed. These tests verify process wiring and locking,
not the correctness or runtime performance of a full projection rebuild.

## Gate before Phase 3

Phase 3 is permitted only after the complete pytest suite, including these cross-phase
tests, passes in CI on this branch.
