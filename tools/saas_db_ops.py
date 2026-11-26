"""Safe PostgreSQL backup, verification and explicitly confirmed restore tools.

Only SAAS_DATABASE_URL is honored. DATABASE_URL is never a fallback.
Connection strings are sent to child processes in PGDATABASE, never argv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class SaasDbOpsError(RuntimeError):
    pass


def database_environment(source=None):
    source = dict(os.environ if source is None else source)
    dsn = str(source.get("SAAS_DATABASE_URL", "")).strip()
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise SaasDbOpsError("SAAS_DATABASE_URL must be a PostgreSQL URL; no other database variable is used.")
    market_dsn = str(source.get("MARKET_DATABASE_URL", "")).strip()
    if market_dsn and dsn == market_dsn:
        raise SaasDbOpsError("SaaS and market database URLs must not be the same.")
    child = dict(source)
    child.pop("SAAS_DATABASE_URL", None)
    child["PGDATABASE"] = dsn
    child["PGCONNECT_TIMEOUT"] = child.get("PGCONNECT_TIMEOUT", "10")
    return child


def _run(command, *, env=None, runner=None):
    result = (runner or subprocess.run)(
        command, env=env, check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SaasDbOpsError("%s failed with exit code %s." % (Path(command[0]).name, result.returncode))
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(backup: Path) -> Path:
    return Path(str(backup) + ".json")


def _write_json_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        if os.name == "posix":
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(payload, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        temp.unlink(missing_ok=True)


def backup_database(directory, *, env=None, runner: Callable | None = None) -> Path:
    pg_env = database_environment(env)
    target_dir = Path(directory).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    if not target_dir.is_dir():
        raise SaasDbOpsError("Backup destination is not a directory.")
    if os.name == "posix":
        target_dir.chmod(stat.S_IRWXU)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / ("saas-" + stamp + ".dump")
    if target.exists() or _manifest_path(target).exists():
        raise SaasDbOpsError("A backup with this timestamp already exists.")
    fd, name = tempfile.mkstemp(prefix=".saas-", suffix=".partial", dir=target_dir)
    os.close(fd)
    temp = Path(name)
    try:
        temp.unlink(missing_ok=True)
        _run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(temp)],
             env=pg_env, runner=runner)
        if not temp.is_file() or temp.stat().st_size < 1:
            raise SaasDbOpsError("pg_dump did not produce a non-empty archive.")
        _run(["pg_restore", "--list", str(temp)], env=pg_env, runner=runner)
        size = temp.stat().st_size
        checksum = _sha256(temp)
        if os.name == "posix":
            temp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp, target)
        _write_json_atomic(_manifest_path(target), {
            "format": "postgresql-custom",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backup_file": target.name,
            "size_bytes": size,
            "sha256": checksum,
        })
        return target
    finally:
        temp.unlink(missing_ok=True)


def verify_backup(backup_file, *, runner: Callable | None = None) -> dict:
    backup = Path(backup_file).expanduser().resolve()
    manifest_path = _manifest_path(backup)
    if not backup.is_file() or not manifest_path.is_file():
        raise SaasDbOpsError("Backup archive or manifest is missing.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SaasDbOpsError("Backup manifest is unreadable.") from None
    if manifest.get("format") != "postgresql-custom" or manifest.get("backup_file") != backup.name:
        raise SaasDbOpsError("Backup manifest does not match the archive.")
    if int(manifest.get("size_bytes", -1)) != backup.stat().st_size:
        raise SaasDbOpsError("Backup size does not match its manifest.")
    if manifest.get("sha256") != _sha256(backup):
        raise SaasDbOpsError("Backup checksum verification failed.")
    _run(["pg_restore", "--list", str(backup)], runner=runner)
    return manifest


def restore_database(backup_file, *, confirmation: str, env=None,
                     runner: Callable | None = None) -> dict:
    if confirmation != "RESTORE_SAAS_DATABASE":
        raise SaasDbOpsError("Restore requires --confirm-restore RESTORE_SAAS_DATABASE.")
    pg_env = database_environment(env)
    manifest = verify_backup(backup_file, runner=runner)
    backup = Path(backup_file).expanduser().resolve()
    _run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
         "--exit-on-error", str(backup)],
        env=pg_env, runner=runner,
    )
    return {"restored": True, "backup_file": backup.name, "sha256": manifest["sha256"]}


def _parser():
    parser = argparse.ArgumentParser(description="Safe operations for the dedicated SaaS PostgreSQL database.")
    subs = parser.add_subparsers(dest="command", required=True)
    backup = subs.add_parser("backup")
    backup.add_argument("--directory", default="backups/saas")
    verify = subs.add_parser("verify")
    verify.add_argument("--file", required=True)
    restore = subs.add_parser("restore")
    restore.add_argument("--file", required=True)
    restore.add_argument("--confirm-restore", default="")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            print(str(backup_database(args.directory)))
        elif args.command == "verify":
            data = verify_backup(args.file)
            print("verified %s (%s bytes)" % (data["backup_file"], data["size_bytes"]))
        else:
            data = restore_database(args.file, confirmation=args.confirm_restore)
            print("restored %s" % data["backup_file"])
        return 0
    except (SaasDbOpsError, OSError) as exc:
        print("saas-db-ops: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
