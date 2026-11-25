#!/usr/bin/env python3
"""Apply or verify ordered SaaS PostgreSQL migrations with checksum protection.

This tool must be run with an explicitly configured migration-owner connection.
The application runtime must use a separate, lower-privilege SAAS_DATABASE_URL.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DIR = ROOT / "saas_core" / "schema"
MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{3}_[a-z0-9_]+)\.sql$")
ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
LOCK_KEY_1 = 20261126
LOCK_KEY_2 = 9


class MigrationError(RuntimeError):
    """A migration is invalid, unsafe, or conflicts with recorded history."""


def load_migrations() -> list[tuple[str, str, str]]:
    migrations: list[tuple[str, str, str]] = []
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(f"Unexpected migration filename: {path.name}")
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"Migration is not UTF-8: {path.name}") from exc
        migrations.append((match.group("version"), hashlib.sha256(raw).hexdigest(), content))
    if not migrations:
        raise MigrationError("No SaaS migrations were found")
    versions = [item[0] for item in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError("Duplicate migration version")
    return migrations


def _expected_database_name() -> str:
    value = os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip()
    if not value:
        raise MigrationError("SAAS_EXPECTED_DATABASE_NAME is required")
    return value


def _connect():
    dsn = os.getenv("SAAS_MIGRATION_DATABASE_URL", "").strip()
    if not dsn:
        raise MigrationError("SAAS_MIGRATION_DATABASE_URL is required; do not use the runtime app role")
    conn = psycopg.connect(dsn, connect_timeout=5, autocommit=True, row_factory=dict_row)
    actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    expected = _expected_database_name()
    if actual != expected:
        conn.close()
        raise MigrationError(
            f"Refusing migration: connected database name {actual!r} does not match "
            "SAAS_EXPECTED_DATABASE_NAME"
        )
    return conn


def _tracking_table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT to_regclass('public.saas_schema_migrations') IS NOT NULL AS present"
    ).fetchone()
    return bool(row["present"])


def _untracked_public_objects_exist(conn) -> bool:
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')) +
             (SELECT COUNT(*) FROM pg_proc p
               JOIN pg_namespace n ON n.oid=p.pronamespace
               WHERE n.nspname='public') AS total"""
    ).fetchone()
    return int(row["total"]) > 0


def _ensure_tracking_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS public.saas_schema_migrations (
             version VARCHAR(120) PRIMARY KEY,
             sha256 CHAR(64) NOT NULL,
             applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
             applied_by TEXT NOT NULL DEFAULT current_user
           )"""
    )


def _recorded_migrations(conn) -> dict[str, str]:
    if not _tracking_table_exists(conn):
        return {}
    rows = conn.execute(
        "SELECT version,sha256 FROM public.saas_schema_migrations ORDER BY version"
    ).fetchall()
    return {row["version"]: row["sha256"].strip() for row in rows}


def _validate_history(migrations: list[tuple[str, str, str]], recorded: dict[str, str]) -> list[tuple[str, str, str]]:
    expected = {version: digest for version, digest, _ in migrations}
    unknown = sorted(set(recorded) - set(expected))
    if unknown:
        raise MigrationError("Database contains unknown migration versions: " + ", ".join(unknown))
    for version, digest in recorded.items():
        if expected[version] != digest:
            raise MigrationError(
                f"Migration {version} changed after application; add a new migration instead"
            )
    return [item for item in migrations if item[0] not in recorded]


def _grant_runtime_role(conn, role_name: str) -> None:
    if not ROLE_NAME.fullmatch(role_name):
        raise MigrationError("Runtime role name must be a simple lowercase PostgreSQL identifier")
    role = sql.Identifier(role_name)
    with conn.transaction():
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (role_name,)
        ).fetchone()
        if not exists:
            raise MigrationError(
                f"Runtime role {role_name!r} must be created out-of-band before granting privileges"
            )
        # Do not grant access to the migration ledger. Future migrations created
        # by this migration owner receive the same bounded DML grants.
        tables = conn.execute(
            """SELECT tablename FROM pg_tables
               WHERE schemaname='public' AND tablename <> 'saas_schema_migrations'"""
        ).fetchall()
        for row in tables:
            conn.execute(
                sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE public.{} TO {}").format(
                    sql.Identifier(row["tablename"]), role
                )
            )
        sequences = conn.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname='public'"
        ).fetchall()
        for row in sequences:
            conn.execute(
                sql.SQL("GRANT USAGE,SELECT ON SEQUENCE public.{} TO {}").format(
                    sql.Identifier(row["sequencename"]), role
                )
            )
        owner = conn.execute("SELECT current_user AS name").fetchone()["name"]
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {}"
            ).format(sql.Identifier(owner), role)
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT USAGE,SELECT ON SEQUENCES TO {}"
            ).format(sql.Identifier(owner), role)
        )


def run(*, apply: bool, grant_role: str | None = None) -> int:
    migrations = load_migrations()
    conn = _connect()
    locked = False
    try:
        if apply:
            conn.execute("SELECT pg_advisory_lock(%s,%s)", (LOCK_KEY_1, LOCK_KEY_2))
            locked = True
            if not _tracking_table_exists(conn):
                if _untracked_public_objects_exist(conn):
                    raise MigrationError(
                        "Existing public schema has no migration ledger. "
                        "Review and adopt it explicitly; refusing to infer applied migrations."
                    )
                _ensure_tracking_table(conn)

        recorded = _recorded_migrations(conn)
        pending = _validate_history(migrations, recorded)

        for version, digest, contents in pending:
            print(f"PENDING {version}")
        if not apply:
            if pending:
                print(f"{len(pending)} migration(s) pending; no database changes made.")
                return 1
            print(f"Schema is current ({len(recorded)} migration(s)); no changes made.")
            return 0

        for version, digest, contents in pending:
            print(f"APPLY {version}")
            with conn.transaction():
                conn.execute(contents, prepare=False)
                conn.execute(
                    """INSERT INTO public.saas_schema_migrations(version,sha256)
                       VALUES (%s,%s)""",
                    (version, digest),
                )
            print(f"APPLIED {version}")

        if grant_role:
            _grant_runtime_role(conn, grant_role)

        final_recorded = _recorded_migrations(conn)
        remaining = _validate_history(migrations, final_recorded)
        if remaining:
            raise MigrationError("Migration application incomplete")
        print(f"Schema current: {len(final_recorded)} migration(s) recorded.")
        return 0
    finally:
        if locked:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s,%s)", (LOCK_KEY_1, LOCK_KEY_2))
            except psycopg.Error:
                pass
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify migration history without changing the database")
    mode.add_argument("--apply", action="store_true", help="apply pending migrations to the explicitly named database")
    parser.add_argument(
        "--grant-role",
        help="grant bounded DML and sequence access to an existing runtime role (never migration-ledger access)",
    )
    args = parser.parse_args(argv)
    try:
        return run(apply=args.apply, grant_role=args.grant_role)
    except (MigrationError, psycopg.Error) as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
