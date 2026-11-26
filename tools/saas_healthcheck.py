"""Readiness probe for the dedicated SaaS PostgreSQL schema."""
from __future__ import annotations

import argparse
import subprocess
import sys

try:
    from tools.saas_db_ops import SaasDbOpsError, database_environment
except ModuleNotFoundError as import_error:
    if import_error.name not in {"tools", "tools.saas_db_ops"}:
        raise
    from saas_db_ops import SaasDbOpsError, database_environment

SQL = """
SELECT CASE WHEN
  to_regclass('public.users') IS NOT NULL AND
  to_regclass('public.sessions') IS NOT NULL AND
  to_regclass('public.effective_user_features') IS NOT NULL AND
  to_regclass('public.analytics_events') IS NOT NULL AND
  to_regclass('public.analytics_anonymous_consents') IS NOT NULL AND
  to_regclass('public.admin_payment_reviews') IS NOT NULL AND
  to_regclass('public.admin_audit_logs') IS NOT NULL AND
  to_regclass('public.withdrawal_requests') IS NOT NULL
THEN 'ready' ELSE 'schema_incomplete' END;
"""


def check_database(*, env=None, runner=None):
    child_env = database_environment(env)
    child_env["PGCONNECT_TIMEOUT"] = "5"
    result = (runner or subprocess.run)(
        ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", SQL],
        env=child_env, check=False, capture_output=True, text=True,
    )
    return result.returncode == 0 and (result.stdout or "").strip() == "ready"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check SaaS PostgreSQL connectivity and schema.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        ready = check_database()
        reason = "database or required schema is not ready"
    except SaasDbOpsError as exc:
        ready, reason = False, str(exc)
    if not args.quiet:
        print("ready" if ready else "not_ready: " + reason)
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
