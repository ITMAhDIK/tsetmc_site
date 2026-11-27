"""Cross-phase integration tests for architecture, HTTP dispatch and refresh worker.

These tests deliberately use disposable files and replace the expensive projection
builder. They exercise the real CLI wiring, SiteApi refresh lock and worker loop
without opening or mutating a production holdings database.
"""
from __future__ import annotations

import sys

import server as server_module
from server import SiteApi
from site_api.process_lock import ProcessLock
from site_api.refresh_worker import main as worker_main


def _worker_args(db, sidecar, lock_file):
    return [
        "--db", str(db),
        "--sidecar", str(sidecar),
        "--run-once",
        "--lock-file", str(lock_file),
    ]


def test_server_cli_external_mode_is_wired_to_the_worker_runtime(
    monkeypatch, tmp_path, capsys
):
    """The CLI selects external mode and passes the configured sidecar unchanged."""
    source_db = tmp_path / "source.sqlite"
    source_db.write_bytes(b"disposable-db-placeholder")
    site_dir = tmp_path / "docs"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    sidecar = tmp_path / "runtime" / "site_data.sqlite"

    instances = []
    runtime_calls = []

    class FakeConnection:
        def close(self):
            pass

    class FakeApi:
        def __init__(self, db, sidecar_path, site, output):
            self.args = (db, sidecar_path, site, output)
            instances.append(self)

        def live(self):
            return FakeConnection()

    class FakeHttpServer:
        server_address = ("127.0.0.1", 8123)

        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "SiteApi", FakeApi)
    monkeypatch.setattr(
        server_module,
        "start_refresh_runtime",
        lambda api, mode, minutes: runtime_calls.append((api, mode, minutes)) or (),
    )
    monkeypatch.setattr(server_module, "_verify_jalali", lambda _live: None)
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", FakeHttpServer)
    monkeypatch.setattr(
        server_module.Handler, "api", None, raising=False
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "server.py",
            "--db", str(source_db),
            "--site-dir", str(site_dir),
            "--output-dir", str(output_dir),
            "--sidecar", str(sidecar),
            "--refresh-mode", "external",
            "--refresh-minutes", "15",
            "--port", "0",
        ],
    )

    server_module.main()

    assert len(instances) == 1
    api = instances[0]
    assert api.args == (
        str(source_db), str(sidecar), str(site_dir), str(output_dir)
    )
    assert runtime_calls == [(api, "external", 15.0)]
    assert server_module.Handler.api is api
    output = capsys.readouterr().out
    assert "external worker mode selected" in output
    assert str(source_db) in output
    assert str(sidecar) in output


def test_external_worker_executes_site_api_refresh_with_the_same_sidecar_lock(
    tmp_path,
):
    """Worker CLI -> SiteApi.refresh -> per-sidecar process lock is one path."""
    source_db = tmp_path / "source.sqlite"
    source_db.write_bytes(b"disposable-db-placeholder")
    sidecar = tmp_path / "runtime" / "site_data.sqlite"
    worker_lock = tmp_path / "worker.lock"
    api = SiteApi(str(source_db), str(sidecar), "", "")
    refresh_calls = []
    api._refresh_locked = lambda: refresh_calls.append(api.sidecar_path)

    factory_calls = []

    def factory(db, sidecar_path, site, output):
        factory_calls.append((db, sidecar_path, site, output))
        return api

    result = worker_main(
        _worker_args(source_db, sidecar, worker_lock),
        api_factory=factory,
    )

    assert result == 0
    assert factory_calls == [(str(source_db), str(sidecar), "", "")]
    assert refresh_calls == [str(sidecar)]
    assert (tmp_path / "runtime" / "site_data.sqlite.refresh.lock").exists()
    assert worker_lock.exists()
    assert api._refresh_error is None


def test_worker_returns_failure_when_api_refresh_cannot_acquire_shared_lock(
    tmp_path,
):
    """The worker must not report success when API-side refresh is skipped."""
    source_db = tmp_path / "source.sqlite"
    source_db.write_bytes(b"disposable-db-placeholder")
    sidecar = tmp_path / "runtime" / "site_data.sqlite"
    worker_lock = tmp_path / "worker.lock"
    api = SiteApi(str(source_db), str(sidecar), "", "")
    refresh_calls = []
    api._refresh_locked = lambda: refresh_calls.append("unexpected")

    with ProcessLock(str(sidecar) + ".refresh.lock"):
        result = worker_main(
            _worker_args(source_db, sidecar, worker_lock),
            api_factory=lambda *_args: api,
        )

    assert result == 1
    assert refresh_calls == []
    assert api._refresh_error is not None
    assert "already held" in api._refresh_error
    assert worker_lock.exists()


def test_worker_propagates_refresh_error_to_nonzero_exit_status(tmp_path):
    """A caught projection failure is surfaced through the worker exit code."""
    source_db = tmp_path / "source.sqlite"
    source_db.write_bytes(b"disposable-db-placeholder")
    sidecar = tmp_path / "runtime" / "site_data.sqlite"
    worker_lock = tmp_path / "worker.lock"
    api = SiteApi(str(source_db), str(sidecar), "", "")

    def fail_refresh():
        api._refresh_error = "controlled projection failure"

    api._refresh_locked = fail_refresh
    result = worker_main(
        _worker_args(source_db, sidecar, worker_lock),
        api_factory=lambda *_args: api,
    )

    assert result == 1
    assert api._refresh_error == "controlled projection failure"
