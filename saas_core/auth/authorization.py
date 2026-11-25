"""Server-side role, permission and subscription entitlement checks."""
from __future__ import annotations
from uuid import UUID

class AuthorizationDenied(Exception):
    """The authenticated principal lacks the requested capability."""

def has_permission(conn, user_id: UUID, permission: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""SELECT EXISTS(
            SELECT 1 FROM user_roles ur
            JOIN role_permissions rp ON rp.role_id=ur.role_id
            JOIN permissions p ON p.id=rp.permission_id
            WHERE ur.user_id=%s AND p.code=%s
        ) AS allowed""", (user_id,permission))
        return bool(cur.fetchone()["allowed"])

def require_permission(conn, user_id: UUID, permission: str) -> None:
    if not has_permission(conn,user_id,permission):
        raise AuthorizationDenied("permission denied")

def has_entitlement(conn, user_id: UUID, entitlement: str) -> bool:
    """Resolve product entitlements independently from administrative roles."""
    if entitlement == "data_management":
        return has_permission(conn,user_id,"market.admin")
    if entitlement not in {"premium_analytics","ownership_graphs","investor_history"}:
        raise ValueError("unknown entitlement")
    with conn.cursor() as cur:
        cur.execute("""SELECT EXISTS(
            SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
            WHERE ur.user_id=%s AND r.code='superadmin'
        ) OR EXISTS(
            SELECT 1 FROM subscriptions s
            JOIN plans p ON p.id=s.plan_id
            WHERE s.user_id=%s AND s.status='active'
              AND s.starts_at<=now() AND s.ends_at>now()
              AND p.code IN ('premium_monthly','premium_quarterly')
        ) AS allowed""",(user_id,user_id))
        return bool(cur.fetchone()["allowed"])

def require_entitlement(conn, user_id: UUID, entitlement: str) -> None:
    if not has_entitlement(conn,user_id,entitlement):
        raise AuthorizationDenied("active subscription required")
