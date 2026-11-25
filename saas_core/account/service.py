"""Account dashboard persistence and policy helpers.

All queries target the dedicated SaaS PostgreSQL database only.
"""
from __future__ import annotations
from uuid import UUID

ALLOWED_PREFERENCES = {
    "theme": {"light", "dark", "system"},
    "calendar": {"jalali", "gregorian"},
    "currency": {"toman", "rial"},
    "locale": {"fa-IR", "en-US"},
}

def get_profile(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT public_id, username, display_name, account_type, status,
                      phone_e164, phone_verified_at, email, email_verified_at,
                      created_at, last_login_at
               FROM users WHERE id=%s""", (user_id,)
        )
        return cur.fetchone()

def update_profile(conn, user_id: UUID, *, display_name: str | None):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE users SET display_name=%s, updated_at=now()
                   WHERE id=%s RETURNING public_id, username, display_name,
                   account_type, status, phone_e164, phone_verified_at,
                   email, email_verified_at, created_at, last_login_at""",
                (display_name, user_id),
            )
            return cur.fetchone()

def get_preferences(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT theme, calendar, currency, locale, updated_at
               FROM user_preferences WHERE user_id=%s""", (user_id,)
        )
        return cur.fetchone()

def update_preferences(conn, user_id: UUID, changes: dict):
    unknown = set(changes) - set(ALLOWED_PREFERENCES)
    if unknown:
        raise ValueError("unsupported preference")
    for key, value in changes.items():
        if value not in ALLOWED_PREFERENCES[key]:
            raise ValueError(f"invalid value for {key}")
    if not changes:
        return get_preferences(conn, user_id)
    assignments = ", ".join(f"{key}=%s" for key in sorted(changes))
    values = [changes[key] for key in sorted(changes)]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE user_preferences SET {assignments}, updated_at=now() "
                "WHERE user_id=%s RETURNING theme, calendar, currency, locale, updated_at",
                (*values, user_id),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError("preferences not found")
            return row

def list_sessions(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, created_at, last_seen_at, expires_at, user_agent,
                      (id = %s) AS is_current
               FROM sessions
               WHERE user_id=%s AND revoked_at IS NULL AND expires_at>now()
               ORDER BY last_seen_at DESC""", (None, user_id)
        )
        return cur.fetchall()

def revoke_other_sessions(conn, user_id: UUID, current_session_id: UUID) -> int:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sessions SET revoked_at=now()
                   WHERE user_id=%s AND id<>%s AND revoked_at IS NULL
                   RETURNING id""", (user_id, current_session_id)
            )
            return len(cur.fetchall())

def revoke_one_session(conn, user_id: UUID, session_id: UUID, current_session_id: UUID) -> bool:
    if session_id == current_session_id:
        raise ValueError("use the logout endpoint to revoke the current session")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sessions SET revoked_at=now()
                   WHERE id=%s AND user_id=%s AND revoked_at IS NULL
                   RETURNING id""", (session_id, user_id)
            )
            return cur.fetchone() is not None

def subscription_history(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id, s.status, s.starts_at, s.ends_at, s.created_at,
                      p.code AS plan_code, p.title AS plan_title,
                      p.duration_days, p.price_toman
               FROM subscriptions s JOIN plans p ON p.id=s.plan_id
               WHERE s.user_id=%s ORDER BY s.created_at DESC""", (user_id,)
        )
        return cur.fetchall()

def feature_access(conn, user_id: UUID):
    from .auth.authorization import has_entitlement
    features = ("premium_analytics", "ownership_graphs", "investor_history")
    return {feature: has_entitlement(conn, user_id, feature) for feature in features}
