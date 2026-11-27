"""Compatibility runtime for inline and external projection refresh modes."""
from __future__ import annotations

import math
import threading
import time


def start_refresh_runtime(
    api,
    mode: str,
    refresh_minutes: float,
    *,
    thread_factory=None,
    sleep=None,
):
    """Start the legacy inline refresher or leave refresh to an external worker.

    The default remains inline for backwards compatibility. In external mode
    this function deliberately starts no thread; the operator must launch the
    site_api.refresh_worker module as a separate process.
    """
    if not math.isfinite(refresh_minutes) or refresh_minutes < 0:
        raise ValueError("refresh_minutes must be a finite non-negative number")
    if mode == "external":
        return ()
    if mode != "inline":
        raise ValueError("mode must be 'inline' or 'external'")

    make_thread = thread_factory or threading.Thread
    sleep_fn = sleep or time.sleep

    initial = make_thread(target=api.refresh, daemon=True)
    initial.start()
    threads = [initial]

    if refresh_minutes > 0:
        def refresh_loop():
            while True:
                sleep_fn(refresh_minutes * 60)
                api.refresh()

        periodic = make_thread(target=refresh_loop, daemon=True)
        periodic.start()
        threads.append(periodic)

    return tuple(threads)
