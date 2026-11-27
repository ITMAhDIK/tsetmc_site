"""Standalone process for rebuilding the site's derived SQLite projections.

Run from the repository root:
    python -m site_api.refresh_worker --db <holdings.sqlite> --sidecar <site_data.sqlite>

The worker does not open an HTTP listener. It reuses the existing SiteApi refresh
implementation, keeping source data read-only and preserving current projection
semantics while refresh execution is moved out of the API process.
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import threading
from pathlib import Path

from server import DEFAULT_DB, SiteApi
from .process_lock import LockAlreadyHeldError, ProcessLock


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIDECAR = str(REPO_ROOT / "site_data.sqlite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh TSETMC Site materialized projections without serving HTTP."
    )
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="path to the source holdings SQLite database (read-only)")
    parser.add_argument("--sidecar", default=DEFAULT_SIDECAR,
                        help="path to the derived site SQLite database")
    parser.add_argument("--refresh-minutes", type=float, default=10,
                        help="wait this many minutes after each completed refresh (0 = once)")
    parser.add_argument("--run-once", action="store_true",
                        help="perform one refresh and exit")
    parser.add_argument("--lock-file", default=None,
                        help="override the worker singleton lock path")
    return parser


def run_refresh_loop(refresh, interval_seconds: float, *, run_once=False,
                     stop_event=None) -> int:
    """Run immediately, then wait between completed refreshes; return exit code."""
    if not math.isfinite(interval_seconds) or interval_seconds < 0:
        raise ValueError("interval_seconds must be a finite non-negative number")
    event = stop_event or threading.Event()
    failed = False

    while True:
        try:
            result = refresh()
            if result is False:
                failed = True
        except Exception as exc:
            failed = True
            print("[refresh-worker] refresh raised: %r" % (exc,), flush=True)

        if run_once or interval_seconds == 0:
            break
        if event.wait(interval_seconds):
            break

    return 1 if failed else 0


def _refresh_api(api) -> bool:
    api.refresh()
    error = getattr(api, "_refresh_error", None)
    if error:
        print("[refresh-worker] refresh failed: %s" % error, flush=True)
        return False
    print("[refresh-worker] refresh completed successfully", flush=True)
    return True


def main(argv=None, *, api_factory=None, stop_event=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.refresh_minutes) or args.refresh_minutes < 0:
        parser.error("--refresh-minutes must be a finite non-negative number")
    if not os.path.isfile(args.db):
        print("[refresh-worker] source database not found: %s" % args.db)
        return 2

    factory = api_factory or SiteApi
    api = factory(args.db, args.sidecar, "", "")
    worker_lock_path = args.lock_file or (args.sidecar + ".worker.lock")
    event = stop_event or threading.Event()
    old_handlers = {}

    # Signal handling is installed only for the actual CLI invocation. Tests
    # that pass argv receive full control of their stop event.
    if argv is None and not args.run_once and args.refresh_minutes > 0:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda _signum, _frame: event.set())
            except (ValueError, OSError):
                old_handlers.pop(signum, None)

    try:
        with ProcessLock(worker_lock_path):
            print("[refresh-worker] starting; db=%s sidecar=%s" %
                  (args.db, args.sidecar), flush=True)
            return run_refresh_loop(
                lambda: _refresh_api(api),
                args.refresh_minutes * 60,
                run_once=args.run_once,
                stop_event=event,
            )
    except LockAlreadyHeldError as exc:
        print("[refresh-worker] another worker is already running: %s" % exc)
        return 3
    finally:
        for signum, handler in old_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
