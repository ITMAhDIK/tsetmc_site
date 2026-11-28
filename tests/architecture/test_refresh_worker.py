"""Tests for the split refresh runtime and standalone worker process."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from server import SiteApi
from site_api.process_lock import LockAlreadyHeldError, ProcessLock
from site_api.refresh_runtime import start_refresh_runtime
from site_api.refresh_worker import main, run_refresh_loop


class FakeThread:
    def __init__(self, *, target, daemon):
        self.target = target
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


class FakeRefreshApi:
    def __init__(self, *args):
        self.args = args
        self.calls = 0
        self._refresh_error = None

    def refresh(self):
        self.calls += 1


class StopAfterWaits:
    def __init__(self, count):
        self.count = count
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return len(self.waits) >= self.count


def test_process_lock_rejects_a_second_owner_and_can_be_reacquired(tmp_path):
    path = tmp_path / "refresh.lock"

    with ProcessLock(path):
        with pytest.raises(LockAlreadyHeldError):
            ProcessLock(path).acquire()

    with ProcessLock(path):
        assert path.exists()


def test_api_refresh_uses_the_same_cross_process_lock_as_external_worker(tmp_path):
    api = SiteApi(
        str(tmp_path / "market.sqlite"),
        str(tmp_path / "site_data.sqlite"),
        str(tmp_path),
        str(tmp_path),
    )
    calls = []
    api._refresh_locked = lambda: calls.append("refresh")

    with ProcessLock(str(api.sidecar_path) + ".refresh.lock"):
        api.refresh()

    assert calls == []
    assert "already held" in api._refresh_error

    api._refresh_error = None
    api.refresh()
    assert calls == ["refresh"]


def test_external_mode_starts_no_inline_refresh_threads():
    api = FakeRefreshApi()
    made = []

    def thread_factory(**kwargs):
        thread = FakeThread(**kwargs)
        made.append(thread)
        return thread

    threads = start_refresh_runtime(
        api, "external", 10, thread_factory=thread_factory
    )

    assert threads == ()
    assert made == []
    assert api.calls == 0


def test_inline_mode_keeps_legacy_initial_and_periodic_refresh_threads():
    api = FakeRefreshApi()
    made = []

    def thread_factory(**kwargs):
        thread = FakeThread(**kwargs)
        made.append(thread)
        return thread

    threads = start_refresh_runtime(
        api, "inline", 10, thread_factory=thread_factory
    )

    assert len(threads) == 2
    assert made == list(threads)
    assert all(thread.started and thread.daemon for thread in threads)
    assert made[0].target == api.refresh


def test_inline_mode_with_zero_interval_runs_only_the_initial_thread():
    api = FakeRefreshApi()
    made = []

    def thread_factory(**kwargs):
        thread = FakeThread(**kwargs)
        made.append(thread)
        return thread

    threads = start_refresh_runtime(
        api, "inline", 0, thread_factory=thread_factory
    )

    assert len(threads) == 1
    assert made[0].target == api.refresh


def test_refresh_runtime_rejects_negative_or_unknown_configuration():
    with pytest.raises(ValueError):
        start_refresh_runtime(FakeRefreshApi(), "inline", -1)
    with pytest.raises(ValueError):
        start_refresh_runtime(FakeRefreshApi(), "invalid", 1)


def test_worker_loop_runs_immediately_and_waits_after_each_refresh():
    calls = []
    stop_event = StopAfterWaits(2)

    result = run_refresh_loop(
        lambda: calls.append("refresh") or True,
        5,
        stop_event=stop_event,
    )

    assert result == 0
    assert calls == ["refresh", "refresh"]
    assert stop_event.waits == [5, 5]


def test_worker_loop_run_once_returns_nonzero_when_refresh_fails():
    calls = []
    result = run_refresh_loop(
        lambda: calls.append("refresh") or False,
        600,
        run_once=True,
        stop_event=StopAfterWaits(0),
    )

    assert result == 1
    assert calls == ["refresh"]


def test_worker_cli_run_once_uses_requested_database_and_sidecar(tmp_path):
    db = tmp_path / "source.sqlite"
    db.write_bytes(b"test-db")
    sidecar = tmp_path / "site_data.sqlite"
    lock = tmp_path / "worker.lock"
    instances = []

    def factory(*args):
        api = FakeRefreshApi(*args)
        instances.append(api)
        return api

    result = main(
        [
            "--db", str(db),
            "--sidecar", str(sidecar),
            "--run-once",
            "--lock-file", str(lock),
        ],
        api_factory=factory,
        stop_event=threading.Event(),
    )

    assert result == 0
    assert len(instances) == 1
    assert instances[0].calls == 1
    assert instances[0].args == (str(db), str(sidecar), "", "")
    assert lock.exists()


def test_worker_cli_rejects_missing_source_database(tmp_path):
    called = []
    result = main(
        ["--db", str(tmp_path / "missing.sqlite")],
        api_factory=lambda *args: called.append(args),
    )

    assert result == 2
    assert called == []


def test_worker_cli_refuses_to_start_when_another_worker_holds_lock(tmp_path):
    db = tmp_path / "source.sqlite"
    db.write_bytes(b"test-db")
    lock = tmp_path / "worker.lock"
    called = []

    with ProcessLock(lock):
        result = main(
            [
                "--db", str(db),
                "--sidecar", str(tmp_path / "site_data.sqlite"),
                "--run-once",
                "--lock-file", str(lock),
            ],
            api_factory=lambda *args: called.append(args),
        )

    assert result == 3
    assert len(called) == 1


def test_source_fingerprint_includes_committed_wal_changes(tmp_path):
    source = tmp_path / "market.sqlite"
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE source_rows (id INTEGER PRIMARY KEY, value TEXT)")
        connection.commit()

        api = SiteApi(
            str(source), str(tmp_path / "site_data.sqlite"),
            str(tmp_path), str(tmp_path),
        )
        first = api._source_fingerprint()
        main_before = source.stat()
        wal_path = tmp_path / "market.sqlite-wal"
        assert wal_path.is_file()

        connection.execute("INSERT INTO source_rows(value) VALUES ('committed-in-wal')")
        connection.commit()

        main_after = source.stat()
        second = api._source_fingerprint()
        assert (main_after.st_size, main_after.st_mtime_ns) == (
            main_before.st_size, main_before.st_mtime_ns
        )
        assert wal_path.stat().st_size > 0
        assert second != first
        assert "wal:" in second
    finally:
        connection.close()
