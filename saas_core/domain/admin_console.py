"""Permission-gated SaaS administration console domain service."""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager


ROLE_CODES = frozenset({"superadmin", "admin", "support", "data_manager", "user"})
USER_STATUSES = frozenset({"active", "suspended"})
SUBSCRIPTION_STATUSES = frozenset({"pending", "active", "expired", "cancelled", "refunded"})
WITHDRAWAL_DECISIONS = frozenset({"approved", "rejected", "paid"})
TICKET_STATUSES = frozenset({"open", "waiting", "answered", "closed"})
TICKET_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


class AdminConsoleError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AdminPermissionDenied(AdminConsoleError):
    def __init__(self, permission: str):
        super().__init__("Permission %s is required." % permission, 403)


class AdminNotFound(AdminConsoleError):
    def __init__(self, resource: str):
        super().__init__("%s was not found." % resource, 404)


class AdminConflict(AdminConsoleError):
    def __init__(self, message: str):
        super().__init__(message, 409)


def _uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise AdminConsoleError("%s must be a valid UUID." % field) from None


def _request_id(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise AdminConsoleError("request_id must be a short identifier.")
    return value


def _text(value, field, *, maximum, required=False):
    if not isinstance(value, str):
        raise AdminConsoleError("%s must be text." % field)
    value = value.strip()
    if required and not value:
        raise AdminConsoleError("%s is required." % field)
    if len(value) > maximum:
        raise AdminConsoleError("%s is too long." % field)
    return value


def _page(limit=50, offset=0, maximum=200):
    def parse(value, field):
        if isinstance(value, bool):
            raise AdminConsoleError("%s must be an integer." % field)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?\d{1,10}", value.strip()):
            return int(value.strip())
        raise AdminConsoleError("%s must be an integer." % field)

    limit_value = parse(limit, "limit")
    offset_value = parse(offset, "offset")
    if not 1 <= limit_value <= maximum or not 0 <= offset_value <= 1_000_000:
        raise AdminConsoleError("Pagination is outside the allowed range.")
    return limit_value, offset_value


def _dict(row, columns):
    if row is None:
        return None
    if isinstance(row, Mapping):
        return {key: row.get(key) for key in columns}
    return dict(zip(columns, row))


def _many(rows, columns):
    return [_dict(row, columns) for row in rows]


class AdminConsoleService:
    """SaaS admin application service; market-data DB is never injected."""

    def __init__(self, *, connection_factory, permission_checker):
        if not callable(connection_factory) or not callable(permission_checker):
            raise ValueError("Database and permission dependencies must be callable.")
        self._connection_factory = connection_factory
        self._permission_checker = permission_checker

    @contextmanager
    def _transaction(self):
        conn = self._connection_factory()
        cursor = None
        try:
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()

    def _actor(self, actor_user_id, permission):
        actor = _uuid(actor_user_id, "actor_user_id")
        if not self._permission_checker(actor, permission):
            raise AdminPermissionDenied(permission)
        return actor

    @staticmethod
    def _audit(cursor, actor, action, target_type, target_id, request_id,
               before=None, after=None):
        cursor.execute(
            """
            INSERT INTO admin_audit_logs
              (actor_user_id, action, target_type, target_id, request_id,
               before_state, after_state, created_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,now())
            """,
            (
                actor, action, target_type, str(target_id), request_id,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
            ),
        )

    @staticmethod
    def _lock_superadmin_role(cursor):
        # Serialize concurrent changes to the superadmin set before counting it.
        cursor.execute("SELECT id FROM roles WHERE code='superadmin' FOR UPDATE")
        if cursor.fetchone() is None:
            raise AdminConsoleError("The superadmin role is missing.", status=500)

    @staticmethod
    def _last_superadmin_guard(cursor, target):
        cursor.execute(
            """
            SELECT COUNT(*) FROM users u
            JOIN user_roles ur ON ur.user_id=u.id
            JOIN roles r ON r.id=ur.role_id
            WHERE r.code='superadmin' AND u.status='active' AND u.id<>%s
            """,
            (target,),
        )
        row = cursor.fetchone()
        count = int(row.get("count", 0) if isinstance(row, Mapping) else row[0])
        if count < 1:
            raise AdminConflict("The last active superadmin cannot be disabled or removed.")

    def overview(self, *, actor_user_id):
        self._actor(actor_user_id, "admin.console.read")
        columns = (
            "users_total", "users_active", "subscriptions_active",
            "payments_pending", "withdrawals_pending", "tickets_open",
            "referrals_pending",
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM users) AS users_total,
                  (SELECT COUNT(*) FROM users WHERE status='active') AS users_active,
                  (SELECT COUNT(*) FROM subscriptions WHERE status='active'
                    AND (starts_at IS NULL OR starts_at<=now())
                    AND (ends_at IS NULL OR ends_at>now())) AS subscriptions_active,
                  (SELECT COUNT(*) FROM payment_orders WHERE status IN ('created','pending')) AS payments_pending,
                  (SELECT COUNT(*) FROM withdrawal_requests WHERE status IN ('requested','approved')) AS withdrawals_pending,
                  (SELECT COUNT(*) FROM tickets WHERE status IN ('open','waiting')) AS tickets_open,
                  (SELECT COUNT(*) FROM referral_invitations WHERE status IN ('pending','verified')) AS referrals_pending
                """
            )
            row = cursor.fetchone()
        return _dict(row, columns) or {key: 0 for key in columns}

    def list_users(self, *, actor_user_id, query=None, status=None, limit=50, offset=0):
        self._actor(actor_user_id, "users.read")
        limit, offset = _page(limit, offset)
        if status is not None and status not in {"active", "suspended", "deleted", "pending"}:
            raise AdminConsoleError("User status is invalid.")
        search = _text(query, "query", maximum=120) if query is not None else ""
        pattern = ("%%%s%%" % search) if search else None
        columns = (
            "id", "public_id", "username", "display_name", "account_type",
            "status", "phone_e164", "email", "created_at", "last_login_at",
            "roles", "total_count",
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT u.id,u.public_id,u.username,u.display_name,u.account_type,
                       u.status,u.phone_e164,u.email,u.created_at,u.last_login_at,
                       COALESCE(array_agg(DISTINCT r.code) FILTER (WHERE r.code IS NOT NULL),
                                ARRAY[]::text[]) AS roles,
                       COUNT(*) OVER() AS total_count
                FROM users u
                LEFT JOIN user_roles ur ON ur.user_id=u.id
                LEFT JOIN roles r ON r.id=ur.role_id
                WHERE (%s IS NULL OR u.status=%s)
                  AND (%s IS NULL OR u.public_id ILIKE %s
                    OR COALESCE(u.username,'') ILIKE %s
                    OR COALESCE(u.display_name,'') ILIKE %s
                    OR COALESCE(u.phone_e164,'') ILIKE %s
                    OR COALESCE(u.email::text,'') ILIKE %s)
                GROUP BY u.id ORDER BY u.created_at DESC,u.id
                LIMIT %s OFFSET %s
                """,
                (status, status, pattern, pattern, pattern, pattern, pattern, pattern, limit, offset),
            )
            rows = cursor.fetchall()
        items = _many(rows, columns)
        return {
            "users": items,
            "total": int(items[0]["total_count"]) if items else 0,
            "limit": limit,
            "offset": offset,
        }

    def user_detail(self, *, actor_user_id, target_user_id):
        self._actor(actor_user_id, "users.read")
        target = _uuid(target_user_id, "target_user_id")
        user_cols = (
            "id", "public_id", "username", "display_name", "account_type", "status",
            "phone_e164", "phone_verified_at", "email", "email_verified_at",
            "created_at", "updated_at", "last_login_at",
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT id,public_id,username,display_name,account_type,status,
                       phone_e164,phone_verified_at,email,email_verified_at,
                       created_at,updated_at,last_login_at
                FROM users WHERE id=%s
                """,
                (target,),
            )
            user = _dict(cursor.fetchone(), user_cols)
            if user is None:
                raise AdminNotFound("User")
            cursor.execute(
                """
                SELECT r.code,r.title,ur.granted_at,ur.granted_by
                FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                WHERE ur.user_id=%s ORDER BY r.code
                """,
                (target,),
            )
            roles = _many(cursor.fetchall(), ("code", "title", "granted_at", "granted_by"))
            cursor.execute(
                """
                SELECT s.id,p.code AS plan_code,p.title AS plan_title,s.status,
                       s.starts_at,s.ends_at,s.created_at
                FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                WHERE s.user_id=%s ORDER BY s.created_at DESC LIMIT 20
                """,
                (target,),
            )
            subs = _many(cursor.fetchall(), ("id", "plan_code", "plan_title", "status", "starts_at", "ends_at", "created_at"))
        return {"user": user, "roles": roles, "subscriptions": subs}

    def set_user_status(self, *, actor_user_id, target_user_id, status, request_id):
        actor = self._actor(actor_user_id, "users.manage")
        target = _uuid(target_user_id, "target_user_id")
        request = _request_id(request_id)
        if not isinstance(status, str) or status not in USER_STATUSES:
            raise AdminConsoleError("Accounts can only be activated or suspended; they are never deleted here.")
        with self._transaction() as cursor:
            cursor.execute("SELECT status FROM users WHERE id=%s FOR UPDATE", (target,))
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("User")
            old_status = row.get("status") if isinstance(row, Mapping) else row[0]
            cursor.execute(
                """
                SELECT EXISTS (SELECT 1 FROM user_roles ur
                  JOIN roles r ON r.id=ur.role_id
                  WHERE ur.user_id=%s AND r.code='superadmin')
                """,
                (target,),
            )
            flag = cursor.fetchone()
            is_superadmin = bool(flag.get("exists") if isinstance(flag, Mapping) else flag[0])
            if is_superadmin and old_status == "active" and status != "active":
                self._lock_superadmin_role(cursor)
                self._last_superadmin_guard(cursor, target)
            cursor.execute(
                "UPDATE users SET status=%s,updated_at=now() WHERE id=%s RETURNING status",
                (status, target),
            )
            result = cursor.fetchone()
            new_status = result.get("status") if isinstance(result, Mapping) else result[0]
            if old_status != new_status and new_status != "active":
                cursor.execute(
                    "UPDATE sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
                    (target,),
                )
            self._audit(cursor, actor, "users.status.change", "user", target, request,
                        {"status": old_status}, {"status": new_status})
        return {"user_id": str(target), "status": new_status}

    def assign_role(self, *, actor_user_id, target_user_id, role_code, request_id):
        actor = self._actor(actor_user_id, "roles.manage")
        target = _uuid(target_user_id, "target_user_id")
        request = _request_id(request_id)
        if not isinstance(role_code, str) or role_code not in ROLE_CODES:
            raise AdminConsoleError("Role is not valid.")
        with self._transaction() as cursor:
            cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (target,))
            if cursor.fetchone() is None:
                raise AdminNotFound("User")
            if role_code != "user":
                cursor.execute(
                    """
                    SELECT EXISTS (SELECT 1 FROM user_roles ur
                      JOIN roles r ON r.id=ur.role_id
                      JOIN users u ON u.id=ur.user_id
                      WHERE ur.user_id=%s AND r.code='superadmin' AND u.status='active')
                    """,
                    (actor,),
                )
                flag = cursor.fetchone()
                is_superadmin = bool(flag.get("exists") if isinstance(flag, Mapping) else flag[0])
                if not is_superadmin:
                    raise AdminPermissionDenied("superadmin role assignment")
            if role_code == "superadmin":
                self._lock_superadmin_role(cursor)
            cursor.execute("SELECT id FROM roles WHERE code=%s", (role_code,))
            role = cursor.fetchone()
            if role is None:
                raise AdminNotFound("Role")
            role_id = role.get("id") if isinstance(role, Mapping) else role[0]
            cursor.execute(
                """
                INSERT INTO user_roles(user_id,role_id,granted_by,granted_at)
                VALUES (%s,%s,%s,now())
                ON CONFLICT(user_id,role_id) DO NOTHING RETURNING user_id
                """,
                (target, role_id, actor),
            )
            inserted = cursor.fetchone() is not None
            if inserted:
                cursor.execute(
                    "UPDATE sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
                    (target,),
                )
                self._audit(cursor, actor, "users.role.assign", "user", target, request,
                            after={"role_code": role_code})
        return {"user_id": str(target), "role_code": role_code, "assigned": inserted}

    def revoke_role(self, *, actor_user_id, target_user_id, role_code, request_id):
        actor = self._actor(actor_user_id, "roles.manage")
        target = _uuid(target_user_id, "target_user_id")
        request = _request_id(request_id)
        if not isinstance(role_code, str) or role_code not in ROLE_CODES:
            raise AdminConsoleError("Role is not valid.")
        with self._transaction() as cursor:
            cursor.execute("SELECT status FROM users WHERE id=%s FOR UPDATE", (target,))
            user = cursor.fetchone()
            if user is None:
                raise AdminNotFound("User")
            user_status = user.get("status") if isinstance(user, Mapping) else user[0]
            if role_code != "user":
                cursor.execute(
                    """
                    SELECT EXISTS (SELECT 1 FROM user_roles ur
                      JOIN roles r ON r.id=ur.role_id
                      JOIN users u ON u.id=ur.user_id
                      WHERE ur.user_id=%s AND r.code='superadmin' AND u.status='active')
                    """,
                    (actor,),
                )
                flag = cursor.fetchone()
                if not bool(flag.get("exists") if isinstance(flag, Mapping) else flag[0]):
                    raise AdminPermissionDenied("superadmin role revocation")
            cursor.execute("SELECT id FROM roles WHERE code=%s", (role_code,))
            role = cursor.fetchone()
            if role is None:
                raise AdminNotFound("Role")
            role_id = role.get("id") if isinstance(role, Mapping) else role[0]
            cursor.execute("SELECT 1 FROM user_roles WHERE user_id=%s AND role_id=%s", (target, role_id))
            exists = cursor.fetchone() is not None
            if exists and role_code == "superadmin" and user_status == "active":
                # Serialize role removals against all other superadmin changes.
                self._lock_superadmin_role(cursor)
                self._last_superadmin_guard(cursor, target)
            cursor.execute("DELETE FROM user_roles WHERE user_id=%s AND role_id=%s", (target, role_id))
            removed = cursor.rowcount > 0
            if removed:
                cursor.execute(
                    "UPDATE sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
                    (target,),
                )
                self._audit(cursor, actor, "users.role.revoke", "user", target, request,
                            before={"role_code": role_code})
        return {"user_id": str(target), "role_code": role_code, "revoked": removed}

    def list_subscriptions(self, *, actor_user_id, status=None, limit=50, offset=0):
        self._actor(actor_user_id, "subscriptions.manage")
        limit, offset = _page(limit, offset)
        if status is not None and (not isinstance(status, str) or status not in SUBSCRIPTION_STATUSES):
            raise AdminConsoleError("Subscription status is invalid.")
        cols = ("id", "user_id", "public_id", "plan_code", "plan_title", "price_toman",
                "status", "starts_at", "ends_at", "created_at")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT s.id,s.user_id,u.public_id,p.code AS plan_code,p.title AS plan_title,
                       p.price_toman,s.status,s.starts_at,s.ends_at,s.created_at
                FROM subscriptions s JOIN users u ON u.id=s.user_id
                JOIN plans p ON p.id=s.plan_id
                WHERE (%s IS NULL OR s.status=%s)
                ORDER BY s.created_at DESC LIMIT %s OFFSET %s
                """,
                (status, status, limit, offset),
            )
            rows = cursor.fetchall()
        return {"subscriptions": _many(rows, cols), "limit": limit, "offset": offset}

    def payment_reconciliation_queue(self, *, actor_user_id, limit=50, offset=0):
        self._actor(actor_user_id, "payments.read")
        limit, offset = _page(limit, offset)
        cols = ("order_id", "public_id", "plan_code", "amount_toman", "order_status",
                "created_at", "attempt_count", "event_count", "last_gateway_status")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT o.id AS order_id,u.public_id,p.code AS plan_code,o.amount_toman,
                       o.status AS order_status,o.created_at,
                       (SELECT COUNT(*) FROM payment_attempts a WHERE a.order_id=o.id) AS attempt_count,
                       (SELECT COUNT(*) FROM gateway_events e WHERE e.order_id=o.id) AS event_count,
                       (SELECT e.processing_status FROM gateway_events e
                         WHERE e.order_id=o.id ORDER BY e.received_at DESC LIMIT 1) AS last_gateway_status
                FROM payment_orders o JOIN users u ON u.id=o.user_id
                LEFT JOIN plans p ON p.id=o.plan_id
                WHERE o.status IN ('created','pending','failed')
                   OR EXISTS (SELECT 1 FROM gateway_events e WHERE e.order_id=o.id
                              AND e.processing_status IN ('received','failed'))
                ORDER BY o.created_at DESC LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
        return {"orders": _many(rows, cols), "limit": limit, "offset": offset}

    def review_payment(self, *, actor_user_id, order_id, outcome, note, request_id):
        actor = self._actor(actor_user_id, "payments.reconcile")
        order = _uuid(order_id, "order_id")
        request = _request_id(request_id)
        if not isinstance(outcome, str) or outcome not in {"matched", "mismatch", "provider_followup", "blocked"}:
            raise AdminConsoleError("Payment review outcome is invalid.")
        note = _text(note, "note", maximum=1000)
        with self._transaction() as cursor:
            cursor.execute("SELECT status FROM payment_orders WHERE id=%s FOR UPDATE", (order,))
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("Payment order")
            order_status = row.get("status") if isinstance(row, Mapping) else row[0]
            cursor.execute(
                """
                INSERT INTO admin_payment_reviews(order_id,actor_user_id,outcome,note,created_at)
                VALUES (%s,%s,%s,%s,now()) RETURNING id
                """,
                (order, actor, outcome, note),
            )
            inserted = cursor.fetchone()
            review_id = inserted.get("id") if isinstance(inserted, Mapping) else inserted[0]
            self._audit(cursor, actor, "payments.reconciliation.review", "payment_order",
                        order, request, before={"order_status": order_status},
                        after={"review_id": str(review_id), "outcome": outcome})
        # A human review is evidence only. It cannot settle payment or activate access.
        return {"review_id": str(review_id), "order_status_unchanged": order_status}

    def wallet_summary(self, *, actor_user_id, target_user_id, limit=50):
        self._actor(actor_user_id, "wallet.read")
        target = _uuid(target_user_id, "target_user_id")
        limit, _ = _page(limit, 0, 100)
        cols = ("cash_balance_toman", "gift_balance_toman",
                "reserved_toman", "available_to_withdraw_toman")
        entry_cols = ("id", "bucket", "entry_type", "amount_toman",
                      "reference_type", "reference_id", "description", "created_at")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(b.cash_balance_toman,0) AS cash_balance_toman,
                       COALESCE(b.gift_balance_toman,0) AS gift_balance_toman,
                       COALESCE(w.reserved_toman,0) AS reserved_toman,
                       COALESCE(w.available_to_withdraw_toman,0) AS available_to_withdraw_toman
                FROM (SELECT %s::uuid AS user_id) t
                LEFT JOIN wallet_balances b ON b.user_id=t.user_id
                LEFT JOIN wallet_withdrawable_balances w ON w.user_id=t.user_id
                """,
                (target,),
            )
            balance = _dict(cursor.fetchone(), cols)
            cursor.execute(
                """
                SELECT id,bucket,entry_type,amount_toman,reference_type,reference_id,description,created_at
                FROM wallet_ledger_entries WHERE user_id=%s
                ORDER BY created_at DESC LIMIT %s
                """,
                (target, limit),
            )
            entries = _many(cursor.fetchall(), entry_cols)
        return {"balances": balance or {key: 0 for key in cols}, "entries": entries}

    def adjust_wallet(
        self, *, actor_user_id, target_user_id, bucket, direction, amount_toman,
        idempotency_key, reason, request_id,
    ):
        actor = self._actor(actor_user_id, "wallet.adjust")
        target = _uuid(target_user_id, "target_user_id")
        request = _request_id(request_id)
        if not isinstance(bucket, str) or bucket not in {"cash", "gift"} or not isinstance(direction, str) or direction not in {"credit", "debit"}:
            raise AdminConsoleError("Wallet bucket or direction is invalid.")
        if not isinstance(amount_toman, int) or isinstance(amount_toman, bool) or not 1 <= amount_toman <= 1_000_000_000:
            raise AdminConsoleError("amount_toman must be a bounded positive integer.")
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise AdminConsoleError("idempotency_key is invalid.")
        reason = _text(reason, "reason", maximum=200, required=True)
        entry_type = "admin_credit" if direction == "credit" else "admin_debit"
        signed_amount = amount_toman if direction == "credit" else -amount_toman
        with self._transaction() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (idempotency_key,))
            cursor.execute(
                """
                SELECT id,user_id,bucket,entry_type,amount_toman
                FROM wallet_ledger_entries WHERE idempotency_key=%s FOR UPDATE
                """,
                (idempotency_key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                item = _dict(existing, ("id", "user_id", "bucket", "entry_type", "amount_toman"))
                matches = (str(item["user_id"]) == str(target) and item["bucket"] == bucket
                           and item["entry_type"] == entry_type
                           and int(item["amount_toman"]) == signed_amount)
                if not matches:
                    raise AdminConflict("idempotency_key was used by a different operation.")
                return {"entry_id": str(item["id"]), "idempotent_replay": True,
                        "amount_toman": signed_amount}
            cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (target,))
            if cursor.fetchone() is None:
                raise AdminNotFound("User")
            cursor.execute(
                "INSERT INTO wallet_accounts(user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (target,),
            )
            cursor.execute("SELECT user_id FROM wallet_accounts WHERE user_id=%s FOR UPDATE", (target,))
            if cursor.fetchone() is None:
                raise AdminConsoleError("Wallet account could not be locked.", 500)
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount_toman) FILTER(WHERE bucket='cash'),0) AS cash_balance,
                       COALESCE(SUM(amount_toman) FILTER(WHERE bucket='gift'),0) AS gift_balance
                FROM wallet_ledger_entries WHERE user_id=%s
                """,
                (target,),
            )
            totals = cursor.fetchone()
            cash, gift = (int(totals.get("cash_balance", 0)), int(totals.get("gift_balance", 0))) if isinstance(totals, Mapping) else (int(totals[0]), int(totals[1]))
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount_toman),0) AS reserved_toman FROM withdrawal_requests
                WHERE user_id=%s AND status IN ('requested','approved')
                """,
                (target,),
            )
            reserved_row = cursor.fetchone()
            reserved = int(reserved_row.get("reserved_toman", 0) if isinstance(reserved_row, Mapping) else reserved_row[0])
            available = cash - reserved if bucket == "cash" else gift
            if direction == "debit" and amount_toman > available:
                raise AdminConflict("Adjustment would make the available %s balance negative." % bucket)
            cursor.execute(
                """
                INSERT INTO wallet_ledger_entries
                  (user_id,bucket,entry_type,amount_toman,reference_type,idempotency_key,description,created_at)
                VALUES (%s,%s,%s,%s,'admin_adjustment',%s,%s,now()) RETURNING id
                """,
                (target, bucket, entry_type, signed_amount, idempotency_key, reason),
            )
            inserted = cursor.fetchone()
            entry_id = inserted.get("id") if isinstance(inserted, Mapping) else inserted[0]
            self._audit(cursor, actor, "wallet.adjustment", "user", target, request,
                        before={"cash": cash, "gift": gift, "reserved_cash": reserved},
                        after={"bucket": bucket, "amount_toman": signed_amount,
                               "entry_id": str(entry_id), "reason": reason})
        return {"entry_id": str(entry_id), "idempotent_replay": False,
                "amount_toman": signed_amount}

    def list_withdrawals(self, *, actor_user_id, status=None, limit=50, offset=0):
        self._actor(actor_user_id, "withdrawals.review")
        limit, offset = _page(limit, offset)
        if status is not None and (not isinstance(status, str) or status not in {"requested", "approved", "rejected", "paid", "cancelled"}):
            raise AdminConsoleError("Withdrawal status is invalid.")
        cols = ("id", "user_id", "public_id", "amount_toman", "status",
                "requested_at", "reviewed_at", "reviewed_by", "paid_at",
                "payout_reference", "review_note")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT w.id,w.user_id,u.public_id,w.amount_toman,w.status,w.requested_at,
                       w.reviewed_at,w.reviewed_by,w.paid_at,w.payout_reference,w.review_note
                FROM withdrawal_requests w JOIN users u ON u.id=w.user_id
                WHERE (%s IS NULL OR w.status=%s)
                ORDER BY w.requested_at DESC LIMIT %s OFFSET %s
                """,
                (status, status, limit, offset),
            )
            rows = cursor.fetchall()
        return {"withdrawals": _many(rows, cols), "limit": limit, "offset": offset}

    def review_withdrawal(self, *, actor_user_id, withdrawal_id, decision,
                          request_id, note=None, payout_reference=None):
        actor = self._actor(actor_user_id, "withdrawals.review")
        withdrawal = _uuid(withdrawal_id, "withdrawal_id")
        request = _request_id(request_id)
        if not isinstance(decision, str) or decision not in WITHDRAWAL_DECISIONS:
            raise AdminConsoleError("Withdrawal decision is invalid.")
        note = _text(note, "note", maximum=500) if note is not None else ""
        payout_ref = _text(payout_reference, "payout_reference", maximum=200, required=True) if decision == "paid" else None
        if decision != "paid" and payout_reference is not None:
            raise AdminConsoleError("payout_reference is only valid for a completed payout.")
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT user_id,status,amount_toman FROM withdrawal_requests WHERE id=%s FOR UPDATE",
                (withdrawal,),
            )
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("Withdrawal")
            data = _dict(row, ("user_id", "status", "amount_toman"))
            transitions = {"requested": {"approved", "rejected"},
                           "approved": {"rejected", "paid"},
                           "rejected": set(), "paid": set(), "cancelled": set()}
            if decision not in transitions.get(data["status"], set()):
                raise AdminConflict("Withdrawal status transition is not allowed.")
            cursor.execute(
                """
                UPDATE withdrawal_requests
                SET status=%s,reviewed_by=%s,reviewed_at=now(),
                    paid_at=CASE WHEN %s='paid' THEN now() ELSE paid_at END,
                    payout_reference=%s,review_note=%s
                WHERE id=%s RETURNING status
                """,
                (decision, actor, decision, payout_ref, note or None, withdrawal),
            )
            changed = cursor.fetchone()
            new_status = changed.get("status") if isinstance(changed, Mapping) else changed[0]
            if decision == "paid":
                cursor.execute(
                    "INSERT INTO wallet_accounts(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                    (data["user_id"],),
                )
                cursor.execute(
                    "SELECT user_id FROM wallet_accounts WHERE user_id=%s FOR UPDATE",
                    (data["user_id"],),
                )
                if cursor.fetchone() is None:
                    raise AdminConsoleError("Wallet account could not be locked.", status=500)
                cursor.execute(
                    """
                    SELECT COALESCE(b.cash_balance_toman,0) AS cash_balance,
                           COALESCE((
                               SELECT SUM(w.amount_toman) FROM withdrawal_requests w
                               WHERE w.user_id=%s AND w.status IN ('requested','approved')
                           ),0) AS other_reservations
                    FROM (SELECT %s::uuid AS user_id) target
                    LEFT JOIN wallet_balances b ON b.user_id=target.user_id
                    """,
                    (data["user_id"], data["user_id"]),
                )
                balances = cursor.fetchone()
                cash_balance = int(balances.get("cash_balance", 0) if isinstance(balances, Mapping) else balances[0])
                other_reservations = int(balances.get("other_reservations", 0) if isinstance(balances, Mapping) else balances[1])
                if cash_balance - int(data["amount_toman"]) < other_reservations:
                    raise AdminConflict("Cash balance is no longer sufficient to complete this payout.")
                cursor.execute(
                    """
                    INSERT INTO wallet_ledger_entries
                      (user_id,bucket,entry_type,amount_toman,reference_type,reference_id,
                       idempotency_key,description,created_at)
                    VALUES (%s,'cash','withdrawal_debit',%s,'withdrawal',%s,%s,%s,now())
                    """,
                    (
                        data["user_id"], -int(data["amount_toman"]), withdrawal,
                        "withdrawal-paid:" + str(withdrawal),
                        "Withdrawal payout " + str(withdrawal),
                    ),
                )
            self._audit(cursor, actor, "withdrawals.review", "withdrawal", withdrawal, request,
                        before={"status": data["status"], "amount_toman": int(data["amount_toman"])},
                        after={"status": new_status, "payout_reference": payout_ref, "note": note})
        return {"withdrawal_id": str(withdrawal), "status": new_status}

    def list_referrals(self, *, actor_user_id, limit=50, offset=0):
        self._actor(actor_user_id, "referrals.read")
        limit, offset = _page(limit, offset)
        cols = ("record_type", "id", "inviter_user_id", "referred_user_id", "status", "created_at")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT record_type,id,inviter_user_id,referred_user_id,status,created_at
                FROM (
                  SELECT 'invitation'::text AS record_type,id,inviter_user_id,
                         invited_user_id AS referred_user_id,status,created_at
                  FROM referral_invitations
                  UNION ALL
                  SELECT 'referral'::text AS record_type,id,inviter_user_id,
                         referred_user_id,status,created_at FROM referrals
                ) activity ORDER BY created_at DESC LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
        return {"referrals": _many(rows, cols), "limit": limit, "offset": offset}

    def list_tickets(self, *, actor_user_id, status=None, limit=50, offset=0):
        self._actor(actor_user_id, "tickets.manage")
        limit, offset = _page(limit, offset)
        if status is not None and (not isinstance(status, str) or status not in TICKET_STATUSES):
            raise AdminConsoleError("Ticket status is invalid.")
        cols = ("id", "ticket_number", "user_id", "user_public_id", "assigned_to",
                "assigned_public_id", "subject", "category", "priority", "status",
                "created_at", "updated_at")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT t.id,t.ticket_number,t.user_id,owner.public_id AS user_public_id,t.assigned_to,
                       assignee.public_id AS assigned_public_id,t.subject,t.category,t.priority,t.status,
                       t.created_at,t.updated_at
                FROM tickets t JOIN users owner ON owner.id=t.user_id
                LEFT JOIN users assignee ON assignee.id=t.assigned_to
                WHERE (%s IS NULL OR t.status=%s)
                ORDER BY t.updated_at DESC LIMIT %s OFFSET %s
                """,
                (status, status, limit, offset),
            )
            rows = cursor.fetchall()
        return {"tickets": _many(rows, cols), "limit": limit, "offset": offset}

    def update_ticket(self, *, actor_user_id, ticket_id, changes, request_id):
        actor = self._actor(actor_user_id, "tickets.manage")
        ticket = _uuid(ticket_id, "ticket_id")
        request = _request_id(request_id)
        if not isinstance(changes, Mapping) or not changes or set(changes) - {"status", "priority", "assigned_to"}:
            raise AdminConsoleError("Ticket changes must contain only status, priority or assigned_to.")
        normalized = {}
        if "status" in changes:
            if not isinstance(changes["status"], str) or changes["status"] not in TICKET_STATUSES:
                raise AdminConsoleError("Ticket status is invalid.")
            normalized["status"] = changes["status"]
        if "priority" in changes:
            if not isinstance(changes["priority"], str) or changes["priority"] not in TICKET_PRIORITIES:
                raise AdminConsoleError("Ticket priority is invalid.")
            normalized["priority"] = changes["priority"]
        if "assigned_to" in changes:
            normalized["assigned_to"] = _uuid(changes["assigned_to"], "assigned_to") if changes["assigned_to"] is not None else None
        with self._transaction() as cursor:
            cursor.execute("SELECT status,priority,assigned_to FROM tickets WHERE id=%s FOR UPDATE", (ticket,))
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("Ticket")
            before = _dict(row, ("status", "priority", "assigned_to"))
            if normalized.get("assigned_to") is not None:
                cursor.execute(
                    """
                    SELECT u.status,EXISTS (
                      SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                      WHERE ur.user_id=u.id AND r.code IN ('superadmin','admin','support')
                    )
                    FROM users u WHERE u.id=%s
                    """,
                    (normalized["assigned_to"],),
                )
                assignee = cursor.fetchone()
                if assignee is None:
                    raise AdminNotFound("Ticket assignee")
                active, support_role = (
                    (assignee.get("status") == "active", bool(assignee.get("exists")))
                    if isinstance(assignee, Mapping) else
                    (assignee[0] == "active", bool(assignee[1]))
                )
                if not active or not support_role:
                    raise AdminConflict("Only active support/admin users may be assigned.")
            assignments = []
            args = []
            for field, value in normalized.items():
                assignments.append(field + "=%s")
                args.append(value)
            assignments.append("updated_at=now()")
            args.append(ticket)
            cursor.execute(
                "UPDATE tickets SET %s WHERE id=%%s RETURNING status,priority,assigned_to" % ",".join(assignments),
                tuple(args),
            )
            after = _dict(cursor.fetchone(), ("status", "priority", "assigned_to"))
            self._audit(cursor, actor, "tickets.update", "ticket", ticket, request,
                        before=dict(before), after=dict(after))
        return {"ticket_id": str(ticket), "ticket": after}

    def add_internal_ticket_note(self, *, actor_user_id, ticket_id, body, request_id):
        actor = self._actor(actor_user_id, "tickets.manage")
        ticket = _uuid(ticket_id, "ticket_id")
        request = _request_id(request_id)
        body = _text(body, "body", maximum=5000, required=True)
        with self._transaction() as cursor:
            cursor.execute("SELECT id FROM tickets WHERE id=%s FOR UPDATE", (ticket,))
            if cursor.fetchone() is None:
                raise AdminNotFound("Ticket")
            cursor.execute(
                """
                INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note,created_at)
                VALUES (%s,%s,%s,TRUE,now()) RETURNING id
                """,
                (ticket, actor, body),
            )
            row = cursor.fetchone()
            message_id = row.get("id") if isinstance(row, Mapping) else row[0]
            self._audit(cursor, actor, "tickets.internal_note.create", "ticket", ticket, request,
                        after={"message_id": str(message_id), "is_internal_note": True})
        return {"ticket_id": str(ticket), "message_id": str(message_id), "internal": True}

    def upsert_faq(self, *, actor_user_id, slug, question, answer, published,
                   sort_order=0, request_id):
        actor = self._actor(actor_user_id, "faq.manage")
        request = _request_id(request_id)
        slug = _text(slug, "slug", maximum=120, required=True)
        question = _text(question, "question", maximum=300, required=True)
        answer = _text(answer, "answer", maximum=20000, required=True)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise AdminConsoleError("FAQ slug must be lowercase letters, numbers and hyphens.")
        if type(published) is not bool:
            raise AdminConsoleError("published must be boolean.")
        if not isinstance(sort_order, int) or isinstance(sort_order, bool) or not -10000 <= sort_order <= 10000:
            raise AdminConsoleError("sort_order is invalid.")
        with self._transaction() as cursor:
            cursor.execute("SELECT question,answer,published,sort_order FROM faq_articles WHERE slug=%s FOR UPDATE", (slug,))
            before = _dict(cursor.fetchone(), ("question", "answer", "published", "sort_order"))
            cursor.execute(
                """
                INSERT INTO faq_articles(slug,question,answer,published,sort_order,updated_at)
                VALUES (%s,%s,%s,%s,%s,now())
                ON CONFLICT(slug) DO UPDATE SET question=EXCLUDED.question,
                  answer=EXCLUDED.answer,published=EXCLUDED.published,
                  sort_order=EXCLUDED.sort_order,updated_at=now()
                RETURNING slug,question,published,sort_order
                """,
                (slug, question, answer, published, sort_order),
            )
            after = _dict(cursor.fetchone(), ("slug", "question", "published", "sort_order"))
            self._audit(cursor, actor, "faq.upsert", "faq_article", slug, request,
                        before=before, after=after)
        return after

    def audit_logs(self, *, actor_user_id, action=None, target_id=None, limit=100, offset=0):
        self._actor(actor_user_id, "audit.read")
        limit, offset = _page(limit, offset)
        if action is not None:
            action = _text(action, "action", maximum=100, required=True)
        if target_id is not None:
            target_id = _text(target_id, "target_id", maximum=120, required=True)
        cols = ("id", "actor_user_id", "action", "target_type", "target_id",
                "request_id", "before_state", "after_state", "created_at")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT id,actor_user_id,action,target_type,target_id,request_id,
                       before_state,after_state,created_at
                FROM admin_audit_logs
                WHERE (%s IS NULL OR action=%s) AND (%s IS NULL OR target_id=%s)
                ORDER BY created_at DESC LIMIT %s OFFSET %s
                """,
                (action, action, target_id, target_id, limit, offset),
            )
            rows = cursor.fetchall()
        return {"entries": _many(rows, cols), "limit": limit, "offset": offset}


    def cancel_subscription(self, *, actor_user_id, subscription_id, request_id):
        """Cancel an existing subscription without ever granting paid access."""
        actor = self._actor(actor_user_id, "subscriptions.manage")
        subscription = _uuid(subscription_id, "subscription_id")
        request = _request_id(request_id)
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT user_id,status,starts_at,ends_at FROM subscriptions WHERE id=%s FOR UPDATE",
                (subscription,),
            )
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("Subscription")
            data = _dict(row, ("user_id", "status", "starts_at", "ends_at"))
            if data["status"] not in {"pending", "active"}:
                raise AdminConflict("Only pending or active subscriptions can be cancelled.")
            cursor.execute(
                """
                UPDATE subscriptions SET status='cancelled',ends_at=GREATEST(COALESCE(starts_at,now()),now())
                WHERE id=%s RETURNING status,ends_at
                """,
                (subscription,),
            )
            updated = _dict(cursor.fetchone(), ("status", "ends_at"))
            self._audit(
                cursor, actor, "subscriptions.cancel", "subscription", subscription, request,
                before={"status": data["status"], "ends_at": data["ends_at"]},
                after={"status": updated["status"], "ends_at": updated["ends_at"]},
            )
        return {"subscription_id": str(subscription), **updated}

    def ticket_detail(self, *, actor_user_id, ticket_id):
        """Return a ticket thread to staff; user-facing APIs must filter internal notes."""
        self._actor(actor_user_id, "tickets.manage")
        ticket = _uuid(ticket_id, "ticket_id")
        ticket_columns = (
            "id", "ticket_number", "user_id", "public_id", "assigned_to",
            "subject", "category", "priority", "status", "created_at", "updated_at",
        )
        message_columns = (
            "id", "sender_user_id", "sender_public_id", "body",
            "is_internal_note", "created_at",
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT t.id,t.ticket_number,t.user_id,u.public_id AS public_id,t.assigned_to,
                       t.subject,t.category,t.priority,t.status,t.created_at,t.updated_at
                FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=%s
                """,
                (ticket,),
            )
            ticket_row = _dict(cursor.fetchone(), ticket_columns)
            if ticket_row is None:
                raise AdminNotFound("Ticket")
            cursor.execute(
                """
                SELECT m.id,m.sender_user_id,u.public_id AS sender_public_id,m.body,m.is_internal_note,m.created_at
                FROM ticket_messages m JOIN users u ON u.id=m.sender_user_id
                WHERE m.ticket_id=%s ORDER BY m.created_at,m.id
                """,
                (ticket,),
            )
            messages = _many(cursor.fetchall(), message_columns)
        return {"ticket": ticket_row, "messages": messages}

    def add_ticket_reply(self, *, actor_user_id, ticket_id, body, request_id):
        actor = self._actor(actor_user_id, "tickets.manage")
        ticket = _uuid(ticket_id, "ticket_id")
        request = _request_id(request_id)
        body = _text(body, "body", maximum=5000, required=True)
        with self._transaction() as cursor:
            cursor.execute("SELECT status FROM tickets WHERE id=%s FOR UPDATE", (ticket,))
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("Ticket")
            previous = row.get("status") if isinstance(row, Mapping) else row[0]
            if previous == "closed":
                raise AdminConflict("A closed ticket cannot receive a staff reply.")
            cursor.execute(
                """
                INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note,created_at)
                VALUES (%s,%s,%s,FALSE,now()) RETURNING id
                """,
                (ticket, actor, body),
            )
            inserted = cursor.fetchone()
            message_id = inserted.get("id") if isinstance(inserted, Mapping) else inserted[0]
            cursor.execute(
                "UPDATE tickets SET status='answered',updated_at=now() WHERE id=%s",
                (ticket,),
            )
            self._audit(
                cursor, actor, "tickets.reply.create", "ticket", ticket, request,
                before={"status": previous},
                after={"status": "answered", "message_id": str(message_id)},
            )
        return {"ticket_id": str(ticket), "message_id": str(message_id), "internal": False}

    def list_faq(self, *, actor_user_id, published=None, limit=100, offset=0):
        self._actor(actor_user_id, "faq.manage")
        limit, offset = _page(limit, offset)
        if published is not None and type(published) is not bool:
            raise AdminConsoleError("published must be a boolean.")
        columns = ("slug", "question", "answer", "published", "sort_order", "updated_at")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT slug,question,answer,published,sort_order,updated_at
                FROM faq_articles
                WHERE (%s IS NULL OR published=%s)
                ORDER BY sort_order,slug LIMIT %s OFFSET %s
                """,
                (published, published, limit, offset),
            )
            rows = cursor.fetchall()
        return {"articles": _many(rows, columns), "limit": limit, "offset": offset}

    def archive_faq(self, *, actor_user_id, slug, request_id):
        actor = self._actor(actor_user_id, "faq.manage")
        slug = _text(slug, "slug", maximum=120, required=True)
        request = _request_id(request_id)
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT published FROM faq_articles WHERE slug=%s FOR UPDATE",
                (slug,),
            )
            row = cursor.fetchone()
            if row is None:
                raise AdminNotFound("FAQ article")
            previous = row.get("published") if isinstance(row, Mapping) else row[0]
            cursor.execute(
                "UPDATE faq_articles SET published=FALSE,updated_at=now() WHERE slug=%s RETURNING published",
                (slug,),
            )
            updated = cursor.fetchone()
            published_now = updated.get("published") if isinstance(updated, Mapping) else updated[0]
            self._audit(
                cursor, actor, "faq.archive", "faq_article", slug, request,
                before={"published": previous}, after={"published": published_now},
            )
        return {"slug": slug, "published": published_now}

    def admin_context(self, *, actor_user_id):
        actor = _uuid(actor_user_id, "actor_user_id")
        columns = ("roles", "permissions")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT
                  COALESCE(array_agg(DISTINCT r.code) FILTER (WHERE r.code IS NOT NULL),ARRAY[]::text[]) AS roles,
                  COALESCE(array_agg(DISTINCT p.code) FILTER (WHERE p.code IS NOT NULL),ARRAY[]::text[]) AS permissions
                FROM users u
                LEFT JOIN user_roles ur ON ur.user_id=u.id
                LEFT JOIN roles r ON r.id=ur.role_id
                LEFT JOIN role_permissions rp ON rp.role_id=r.id
                LEFT JOIN permissions p ON p.id=rp.permission_id
                WHERE u.id=%s AND u.status='active'
                GROUP BY u.id
                """,
                (actor,),
            )
            result = _dict(cursor.fetchone(), columns)
        if result is None:
            raise AdminPermissionDenied("active staff session")
        return {"user_id": str(actor), "roles": result["roles"], "permissions": result["permissions"]}



    def list_roles(self, *, actor_user_id):
        """List server-defined roles and permissions for superadmin role management."""
        self._actor(actor_user_id, "roles.manage")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT r.code,r.title,
                       COALESCE(array_agg(DISTINCT p.code)
                         FILTER (WHERE p.code IS NOT NULL),ARRAY[]::text[]) AS permissions
                FROM roles r
                LEFT JOIN role_permissions rp ON rp.role_id=r.id
                LEFT JOIN permissions p ON p.id=rp.permission_id
                GROUP BY r.id,r.code,r.title
                ORDER BY CASE r.code
                    WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1
                    WHEN 'support' THEN 2 WHEN 'data_manager' THEN 3 ELSE 4 END
                """
            )
            rows=cursor.fetchall()
        return {"roles":_many(rows,("code","title","permissions"))}

