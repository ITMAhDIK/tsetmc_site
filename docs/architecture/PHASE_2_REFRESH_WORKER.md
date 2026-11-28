# Phase 2 — External data-refresh worker

Branch: refactor/architecture-stabilization-phase0  
Status: implemented behind an explicit runtime mode; legacy inline mode remains the default.

## Goal

Move the expensive materialized-summary refresh out of the HTTP API process without
rewriting the existing projection builder, changing the source database, or changing
the API response contract.

## Delivered

- site_api.refresh_worker: standalone process with one-shot and periodic modes.
- site_api.refresh_runtime: explicit selection between legacy inline refresh and external-worker mode.
- site_api.process_lock: portable advisory OS lock for process-level exclusion.
- SiteApi.refresh() now uses a per-sidecar .refresh.lock, so an inline API process,
  an external worker, and an accidental second invocation cannot rebuild the sidecar
  concurrently.
- The worker has a separate .worker.lock to prevent duplicate scheduler processes.
- The API accepts --sidecar and --refresh-mode inline|external.
- Default mode remains inline to preserve existing local startup behavior.

## Run in external mode

Terminal 1 — API only (no refresh thread is started):

```powershell
python server.py --db "D:\data\tsetmc_holdings.sqlite" --sidecar ".\site_data.sqlite" --refresh-mode external
```

Terminal 2 — refresh worker:

```powershell
python -m site_api.refresh_worker --db "D:\data\tsetmc_holdings.sqlite" --sidecar ".\site_data.sqlite" --refresh-minutes 10
```

The worker performs a refresh immediately on startup, then waits ten minutes after
each completed refresh before starting the next pass. Use --run-once or
--refresh-minutes 0 for a one-pass run. The worker does not serve HTTP.

The API remains available during refresh, but endpoints requiring materialized data
continue to return the existing not-ready response until the sidecar's version and
completion markers are valid.

## Locking and safety

- The source holdings DB continues to be opened through SQLite mode=ro and
  PRAGMA query_only=ON.
- The refresh fingerprint includes the source DB file and any WAL containing
  frames (size + nanosecond mtime). Missing, zero-byte, and 32-byte header-only
  WAL files are normalized to the same `wal:empty` state because they contain no
  frames; this avoids false refreshes when SQLite removes an empty WAL on close.
- A committed transaction may live only in WAL without changing the main DB file;
  a WAL with frames must still invalidate the Sidecar.
- Both runtime modes use the same sidecar path and the same per-sidecar refresh lock.
- A concurrent refresher is skipped rather than running a second full rebuild.
- OS locks are released automatically when a process exits or crashes.
- Lock files are not deleted after release; deleting a lock inode can create races
  between processes opening the same lock path.
- Refresh failures are logged. A one-shot worker exits with code 1 on failure;
  a long-running worker retries on its next scheduled interval.
- Worker singleton lock contention exits with code 3; a missing source database exits
  with code 2.

## Rollback

Stop the external worker and restart the API without --refresh-mode external
(or explicitly with --refresh-mode inline). The default remains inline mode.
The process-level lock prevents overlapping work during a transition.

## Verification

The automated tests cover process-lock exclusion/reacquisition, protection against
API/worker refresh overlap, inline-versus-external runtime selection, periodic-loop
timing, one-shot failure status, missing DB handling, duplicate-worker rejection,
the worker CLI using the requested DB/sidecar paths, and WAL-only source changes
that leave the main DB file's size/mtime unchanged.

These tests use disposable files and fake refresh APIs. They do not scan or mutate
the production holdings database, run a real multi-million-row refresh, or validate
a production deployment.


## CI result

GitHub Actions passed on this implementation:
- Push run: [45883525545](https://github.com/ITMAhDIK/tsetmc_site/actions/runs/45883525545) — **135 passed in 3.86s**.
- Pull-request run: [45883532727](https://github.com/ITMAhDIK/tsetmc_site/actions/runs/45883532727) — **135 passed in 3.9s**.

The passing suite includes the Phase 0 API-contract tests, Phase 1 HTTP-handler tests, and the new Phase 2 worker/locking tests.
