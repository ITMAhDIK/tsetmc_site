from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import saas_db_ops
from tools.saas_db_ops import SaasDbOpsError
from tools.saas_healthcheck import check_database
from tools.readonly_load_test import LoadTestError, run_readonly_load, validate_target


def test_database_connection_uses_only_saas_database_environment():
    env = {
        "SAAS_DATABASE_URL": "postgresql://saas:test@localhost/saas",
        "DATABASE_URL": "postgresql://market:secret@localhost/market",
    }
    child = saas_db_ops.database_environment(env)
    assert child["PGDATABASE"] == "saas"
    assert child["PGHOST"] == "localhost"
    assert child["PGUSER"] == "saas"
    assert child["PGPASSWORD"] == "test"
    assert "SAAS_DATABASE_URL" not in child
    assert "DATABASE_URL" not in child


def test_database_environment_refuses_missing_or_shared_dsn():
    with pytest.raises(SaasDbOpsError):
        saas_db_ops.database_environment({"DATABASE_URL": "postgresql://market/db"})
    dsn = "postgresql://same/db"
    with pytest.raises(SaasDbOpsError):
        saas_db_ops.database_environment({"SAAS_DATABASE_URL": dsn, "MARKET_DATABASE_URL": dsn})


def test_backup_verify_and_restore_are_private_and_keep_dsn_out_of_argv(tmp_path):
    calls = []
    dsn = "postgresql://saas:secret@localhost/saas"
    def runner(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[0] == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"custom-archive")
        return SimpleNamespace(returncode=0, stdout="toc", stderr="")

    backup = saas_db_ops.backup_database(
        tmp_path / "private", env={"SAAS_DATABASE_URL": dsn}, runner=runner
    )
    assert backup.is_file()
    assert json.loads(Path(str(backup) + ".json").read_text())["sha256"]
    assert saas_db_ops.verify_backup(backup, runner=runner)["backup_file"] == backup.name

    with pytest.raises(SaasDbOpsError):
        saas_db_ops.restore_database(backup, confirmation="yes", env={"SAAS_DATABASE_URL": dsn}, runner=runner)

    result = saas_db_ops.restore_database(
        backup, confirmation="RESTORE_SAAS_DATABASE",
        env={"SAAS_DATABASE_URL": dsn}, runner=runner,
    )
    assert result["restored"] is True
    assert any(cmd[0] == "pg_restore" and "--clean" in cmd for cmd, _ in calls)
    for command, kwargs in calls:
        assert dsn not in command
        if kwargs["env"] is not None:
            assert kwargs["env"]["PGDATABASE"] == "saas"
            assert "SAAS_DATABASE_URL" not in kwargs["env"]


def test_backup_checksum_tampering_blocks_restore_before_pg_restore(tmp_path):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        if command[0] == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"archive")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    backup = saas_db_ops.backup_database(
        tmp_path, env={"SAAS_DATABASE_URL": "postgresql://localhost/saas"}, runner=runner
    )
    backup.write_bytes(b"archivE")
    with pytest.raises(SaasDbOpsError, match="checksum"):
        saas_db_ops.restore_database(
            backup, confirmation="RESTORE_SAAS_DATABASE",
            env={"SAAS_DATABASE_URL": "postgresql://localhost/saas"}, runner=runner
        )
    assert not any(cmd[0] == "pg_restore" and "--clean" in cmd for cmd in calls)


def test_healthcheck_does_not_put_database_url_in_command_line():
    dsn = "postgresql://saas:secret@localhost/saas"
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ready\n", stderr="")
    assert check_database(env={"SAAS_DATABASE_URL": dsn}, runner=runner)
    command, kwargs = calls[0]
    assert dsn not in command
    assert kwargs["env"]["PGDATABASE"] == "saas"
    assert kwargs["env"]["PGHOST"] == "localhost"
    assert kwargs["env"]["PGUSER"] == "saas"
    assert kwargs["env"]["PGPASSWORD"] == "secret"
    assert "admin_payment_reviews" in command[-1]


def test_healthcheck_returns_false_on_incomplete_schema():
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="schema_incomplete\n", stderr="")
    assert not check_database(env={"SAAS_DATABASE_URL": "postgresql://localhost/saas"}, runner=runner)


def test_staging_load_test_rejects_production_and_mutating_targets():
    with pytest.raises(LoadTestError):
        validate_target("https://tloomberg.com/api/v1/market/overview",
                        environment="staging", acknowledgement="STAGING-ONLY")
    with pytest.raises(LoadTestError):
        validate_target("https://staging.example.com/api/v1/admin/users",
                        environment="staging", acknowledgement="STAGING-ONLY")
    with pytest.raises(LoadTestError):
        validate_target("https://staging.example.com/api/v1/market/overview",
                        environment="production", acknowledgement="STAGING-ONLY")


def test_staging_load_test_is_get_only_bounded_and_reports_latency():
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size): return b'{"ready":true}'
    def opener(request, timeout):
        seen.append((request.method, timeout, request.headers.get("Authorization")))
        return Response()

    result = run_readonly_load(
        "https://staging.example.com/health/ready",
        requests=12, concurrency=3, timeout=2, bearer_token="test-token",
        opener=opener,
    )
    assert result["successes"] == 12 and result["failures"] == 0
    assert result["p95_ms"] >= 0
    assert len(seen) == 12
    assert all(method == "GET" and timeout == 2 for method, timeout, _ in seen)
    assert all(auth == "Bearer test-token" for _, _, auth in seen)


def test_staging_load_test_caps_request_count_and_concurrency():
    with pytest.raises(LoadTestError):
        run_readonly_load("https://staging.example.com/health/ready", requests=50000)


def test_database_environment_maps_ssl_mode_without_exposing_url():
    child = saas_db_ops.database_environment({
        "SAAS_DATABASE_URL": "postgresql://svc:p%40ss@db.example:6432/saas?sslmode=require"
    })
    assert child["PGHOST"] == "db.example"
    assert child["PGPORT"] == "6432"
    assert child["PGDATABASE"] == "saas"
    assert child["PGPASSWORD"] == "p@ss"
    assert child["PGSSLMODE"] == "require"
    assert not any("postgresql://" in value for value in child.values())


def test_database_environment_refuses_same_database_with_different_credentials():
    with pytest.raises(SaasDbOpsError):
        saas_db_ops.database_environment({
            "SAAS_DATABASE_URL": "postgresql://svc-a:secret@db.example:5432/saas",
            "MARKET_DATABASE_URL": "postgresql://svc-b:other@db.example:5432/saas",
        })
