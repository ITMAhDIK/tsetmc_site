"""Superadmin console read/write services.

Mutations use row locks, append-only financial records, and audit logs. Payment
and payout facts are never fabricated: reconciliation notes document findings
without changing gateway verification state.
"""
from __future__ import annotations
import json
from uuid import UUID
from saas_core.wallet.wallet_repository import _lock_wallet, get_wallet_balances, post_ledger_entry


def _audit(conn, *, actor_id: UUID, action: str, target_type: str,
           target_id: str, before: dict | None = None, after: dict | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO admin_audit_logs
               (actor_user_id,action,target_type,target_id,before_state,after_state)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (actor_id,action,target_type,target_id,
             json.dumps(before,default=str) if before is not None else None,
             json.dumps(after,default=str) if after is not None else None),
        )


def dashboard_summary(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 (SELECT COUNT(*) FROM users)::bigint AS total_users,
                 (SELECT COUNT(*) FROM users WHERE status='active')::bigint AS active_users,
                 (SELECT COUNT(*) FROM users WHERE created_at>=now()-interval '30 days')::bigint AS new_users_30d,
                 (SELECT COUNT(*) FROM subscriptions
                   WHERE status='active' AND starts_at<=now() AND ends_at>now())::bigint AS active_subscriptions,
                 (SELECT COUNT(*) FROM payment_orders WHERE status IN ('created','pending'))::bigint AS pending_payment_orders,
                 (SELECT COUNT(*) FROM payment_orders WHERE status='paid'
                   AND paid_at>=now()-interval '30 days')::bigint AS paid_orders_30d,
                 (SELECT COALESCE(SUM(amount_toman),0) FROM payment_orders WHERE status='paid'
                   AND paid_at>=now()-interval '30 days')::bigint AS gross_paid_toman_30d,
                 (SELECT COUNT(*) FROM withdrawal_requests
                   WHERE status='requested')::bigint AS pending_withdrawals,
                 (SELECT COUNT(*) FROM tickets WHERE status IN ('open','waiting'))::bigint AS open_tickets,
                 (SELECT COUNT(*) FROM referrals WHERE status='pending')::bigint AS pending_referrals,
                 (SELECT COUNT(*) FROM analytics_events
                   WHERE occurred_at>=now()-interval '30 days')::bigint AS analytics_events_30d"""
        )
        return cur.fetchone()


def list_users(conn, *, query: str | None, status: str | None,
               limit: int = 50, offset: int = 0):
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise ValueError("invalid pagination")
    if status is not None and status not in {"active","suspended","pending","deleted"}:
        raise ValueError("invalid user status")
    clauses=[]
    params=[]
    if status:
        clauses.append("u.status=%s")
        params.append(status)
    if query:
        q=query.strip()
        if len(q)>80:
            raise ValueError("search query is too long")
        clauses.append("(u.username ILIKE %s OR u.public_id ILIKE %s OR u.display_name ILIKE %s)")
        pattern=f"%{q}%"
        params.extend([pattern,pattern,pattern])
    where=("WHERE "+" AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT u.id,u.public_id,u.username,u.display_name,u.account_type,u.status,
                       u.created_at,u.last_login_at,
                       COALESCE(array_agg(DISTINCT r.code) FILTER (WHERE r.code IS NOT NULL),'{{}}') AS roles
                FROM users u LEFT JOIN user_roles ur ON ur.user_id=u.id
                LEFT JOIN roles r ON r.id=ur.role_id
                {where}
                GROUP BY u.id ORDER BY u.created_at DESC,u.id
                LIMIT %s OFFSET %s""",
            (*params,limit,offset),
        )
        users=cur.fetchall()
        cur.execute(f"SELECT COUNT(*)::bigint AS total FROM users u {where}",params)
        total=cur.fetchone()["total"]
    return {"items":users,"total":total,"limit":limit,"offset":offset}


def get_user_detail(conn, *, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,public_id,username,display_name,account_type,status,
                      phone_verified_at,email_verified_at,created_at,updated_at,last_login_at
               FROM users WHERE id=%s""",(user_id,)
        )
        user=cur.fetchone()
        if not user:
            return None
        cur.execute(
            """SELECT r.code,r.title,ur.granted_at,ur.granted_by
               FROM user_roles ur JOIN roles r ON r.id=ur.role_id
               WHERE ur.user_id=%s ORDER BY r.code""",(user_id,)
        )
        user["roles"]=cur.fetchall()
        cur.execute(
            """SELECT id,created_at,last_seen_at,expires_at,revoked_at
               FROM sessions WHERE user_id=%s ORDER BY created_at DESC LIMIT 20""",(user_id,)
        )
        user["sessions"]=cur.fetchall()
        cur.execute(
            """SELECT s.id,s.status,s.starts_at,s.ends_at,p.code AS plan_code,p.title AS plan_title
               FROM subscriptions s JOIN plans p ON p.id=s.plan_id
               WHERE s.user_id=%s ORDER BY s.created_at DESC LIMIT 20""",(user_id,)
        )
        user["subscriptions"]=cur.fetchall()
        cur.execute(
            """SELECT essential,product_analytics,consent_version,updated_at
               FROM analytics_consents WHERE user_id=%s""",(user_id,)
        )
        user["analytics_consent"]=cur.fetchone()
        return user


def change_user_status(conn, *, actor_id: UUID, user_id: UUID, status: str, reason: str):
    if status not in {"active","suspended"}:
        raise ValueError("only active or suspended status is allowed")
    reason=reason.strip()
    if not 3 <= len(reason) <= 500:
        raise ValueError("reason must contain 3-500 characters")
    if actor_id == user_id:
        raise ValueError("self-suspension/reactivation is not allowed")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM users WHERE id=%s FOR UPDATE",(user_id,))
            before=cur.fetchone()
            if not before:
                raise LookupError("user not found")
            cur.execute(
                """SELECT EXISTS(
                     SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                     WHERE ur.user_id=%s AND r.code='superadmin'
                   ) AS is_superadmin""",(user_id,)
            )
            is_superadmin=cur.fetchone()["is_superadmin"]
            if is_superadmin and status=="suspended":
                # Serialize all superadmin suspension decisions, not merely the
                # target row, so two concurrent requests cannot remove the last
                # active superadmins at the same time.
                cur.execute(
                    """SELECT u.id FROM users u
                       JOIN user_roles ur ON ur.user_id=u.id
                       JOIN roles r ON r.id=ur.role_id
                       WHERE r.code='superadmin' ORDER BY u.id FOR UPDATE OF u"""
                )
                cur.fetchall()
                cur.execute(
                    """SELECT COUNT(*)::integer AS total FROM users u
                       JOIN user_roles ur ON ur.user_id=u.id
                       JOIN roles r ON r.id=ur.role_id
                       WHERE u.status='active' AND r.code='superadmin'""")
                active_superadmins=cur.fetchone()["total"]
                if active_superadmins <= 1:
                    raise ValueError("the last active superadmin cannot be suspended")
            cur.execute(
                "UPDATE users SET status=%s,updated_at=now() WHERE id=%s RETURNING id,status",
                (status,user_id),
            )
            updated=cur.fetchone()
            if status=="suspended":
                cur.execute(
                    "UPDATE sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
                    (user_id,),
                )
            _audit(conn,actor_id=actor_id,action="superadmin.user.status",
                   target_type="user",target_id=str(user_id),
                   before={"status":before["status"]},
                   after={"status":status,"reason":reason})
            return updated


def list_roles(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.code,r.title,COUNT(DISTINCT ur.user_id)::bigint AS assigned_users,
                      COALESCE(array_agg(DISTINCT p.code) FILTER (WHERE p.code IS NOT NULL),'{}') AS permissions
               FROM roles r LEFT JOIN user_roles ur ON ur.role_id=r.id
               LEFT JOIN role_permissions rp ON rp.role_id=r.id
               LEFT JOIN permissions p ON p.id=rp.permission_id
               GROUP BY r.id ORDER BY r.code"""
        )
        return cur.fetchall()


def change_user_role(conn, *, actor_id: UUID, user_id: UUID,
                     role_code: str, grant: bool):
    # Superadmin bootstrap/removal remains an out-of-band break-glass operation.
    if role_code not in {"admin","support"}:
        raise ValueError("only admin and support roles can be managed in this console")
    if actor_id == user_id:
        raise ValueError("self role changes are not allowed")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM users WHERE id=%s FOR UPDATE",(user_id,))
            target=cur.fetchone()
            if not target:
                raise LookupError("user not found")
            if grant and target["status"]!="active":
                raise ValueError("administrative roles can only be granted to active users")
            cur.execute("SELECT id FROM roles WHERE code=%s",(role_code,))
            role=cur.fetchone()
            if not role:
                raise LookupError("role not found")
            if grant:
                cur.execute(
                    """INSERT INTO user_roles(user_id,role_id,granted_by)
                       VALUES (%s,%s,%s) ON CONFLICT(user_id,role_id) DO NOTHING""",
                    (user_id,role["id"],actor_id),
                )
            else:
                cur.execute(
                    "DELETE FROM user_roles WHERE user_id=%s AND role_id=%s RETURNING user_id",
                    (user_id,role["id"]),
                )
            _audit(conn,actor_id=actor_id,action="superadmin.user.role",
                   target_type="user",target_id=str(user_id),
                   after={"role":role_code,"granted":grant})
            return {"user_id":user_id,"role":role_code,"granted":grant}


def list_plans(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,code,title,duration_days,price_toman,active,created_at
               FROM plans ORDER BY price_toman,code"""
        )
        return cur.fetchall()


def update_plan(conn, *, actor_id: UUID, plan_id: UUID, changes: dict):
    allowed={"title","duration_days","price_toman","active"}
    if not changes or set(changes)-allowed:
        raise ValueError("provide supported plan fields")
    if "title" in changes and (
        not isinstance(changes["title"],str) or not 2<=len(changes["title"].strip())<=100
    ):
        raise ValueError("invalid plan title")
    if "duration_days" in changes and (
        isinstance(changes["duration_days"],bool) or not isinstance(changes["duration_days"],int)
        or not 1<=changes["duration_days"]<=3650
    ):
        raise ValueError("duration_days must be between 1 and 3650")
    if "price_toman" in changes and (
        isinstance(changes["price_toman"],bool) or not isinstance(changes["price_toman"],int)
        or not 0<=changes["price_toman"]<=10**12
    ):
        raise ValueError("price_toman is out of range")
    if "active" in changes and not isinstance(changes["active"],bool):
        raise TypeError("active must be boolean")
    columns=sorted(changes)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT title,duration_days,price_toman,active FROM plans WHERE id=%s FOR UPDATE",(plan_id,))
            before=cur.fetchone()
            if not before:
                raise LookupError("plan not found")
            assignments=", ".join(f"{column}=%s" for column in columns)
            values=[changes[column].strip() if column=="title" else changes[column] for column in columns]
            cur.execute(
                f"UPDATE plans SET {assignments} WHERE id=%s RETURNING id,code,title,duration_days,price_toman,active",
                (*values,plan_id),
            )
            updated=cur.fetchone()
            _audit(conn,actor_id=actor_id,action="superadmin.plan.update",
                   target_type="plan",target_id=str(plan_id),
                   before=before,after=updated)
            return updated


def list_subscriptions(conn, *, status: str | None, limit: int = 50, offset: int = 0):
    allowed={"pending","active","expired","cancelled","refunded"}
    if status is not None and status not in allowed:
        raise ValueError("invalid subscription status")
    if not 1<=limit<=100 or not 0<=offset<=10_000:
        raise ValueError("invalid pagination")
    where="WHERE s.status=%s" if status else ""
    params=[status] if status else []
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT s.id,s.user_id,u.username,s.status,s.starts_at,s.ends_at,s.created_at,
                       p.code AS plan_code,p.title AS plan_title,p.price_toman,
                       s.payment_order_id
                FROM subscriptions s JOIN users u ON u.id=s.user_id
                JOIN plans p ON p.id=s.plan_id {where}
                ORDER BY s.created_at DESC LIMIT %s OFFSET %s""",
            (*params,limit,offset),
        )
        return cur.fetchall()


def cancel_subscription(conn, *, actor_id: UUID, subscription_id: UUID, reason: str):
    reason=reason.strip()
    if not 3<=len(reason)<=500:
        raise ValueError("reason must contain 3-500 characters")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id,status,ends_at FROM subscriptions WHERE id=%s FOR UPDATE",
                (subscription_id,),
            )
            before=cur.fetchone()
            if not before:
                raise LookupError("subscription not found")
            if before["status"] not in {"active","pending"}:
                raise ValueError("only active or pending subscriptions can be cancelled")
            cur.execute(
                "UPDATE subscriptions SET status='cancelled' WHERE id=%s RETURNING id,user_id,status,ends_at",
                (subscription_id,),
            )
            updated=cur.fetchone()
            _audit(conn,actor_id=actor_id,action="superadmin.subscription.cancel",
                   target_type="subscription",target_id=str(subscription_id),
                   before={"status":before["status"]},
                   after={"status":"cancelled","reason":reason})
            return updated


def list_payment_orders(conn, *, status: str | None, limit: int = 50, offset: int = 0):
    allowed={"created","pending","paid","failed","cancelled","refunded"}
    if status is not None and status not in allowed:
        raise ValueError("invalid payment status")
    if not 1<=limit<=100 or not 0<=offset<=10_000:
        raise ValueError("invalid pagination")
    where="WHERE o.status=%s" if status else ""
    params=[status] if status else []
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT o.id,o.user_id,u.username,o.status,o.amount_toman,o.currency,
                       o.created_at,o.paid_at,o.expires_at,p.code AS plan_code,p.title AS plan_title,
                       latest.provider,latest.attempt_status,latest.provider_order_id
                FROM payment_orders o JOIN users u ON u.id=o.user_id
                LEFT JOIN plans p ON p.id=o.plan_id
                LEFT JOIN LATERAL (
                  SELECT a.provider,a.status AS attempt_status,a.provider_order_id
                  FROM payment_attempts a WHERE a.order_id=o.id
                  ORDER BY a.created_at DESC LIMIT 1
                ) latest ON TRUE
                {where} ORDER BY o.created_at DESC LIMIT %s OFFSET %s""",
            (*params,limit,offset),
        )
        return cur.fetchall()


def add_payment_reconciliation_note(conn, *, actor_id: UUID, order_id: UUID,
                                    note: str, idempotency_key: str):
    note=note.strip()
    if not 3<=len(note)<=2000:
        raise ValueError("note must contain 3-2000 characters")
    if not idempotency_key or len(idempotency_key)>120:
        raise ValueError("valid idempotency key is required")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM payment_orders WHERE id=%s FOR UPDATE",(order_id,))
            order=cur.fetchone()
            if not order:
                raise LookupError("payment order not found")
            cur.execute(
                """INSERT INTO payment_reconciliation_notes
                   (order_id,actor_user_id,observed_status,note,idempotency_key)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(idempotency_key) DO NOTHING
                   RETURNING id,order_id,actor_user_id,observed_status,note,created_at""",
                (order_id,actor_id,order["status"],note,idempotency_key),
            )
            row=cur.fetchone()
            if not row:
                cur.execute(
                    """SELECT id,order_id,actor_user_id,observed_status,note,created_at
                       FROM payment_reconciliation_notes WHERE idempotency_key=%s""",
                    (idempotency_key,),
                )
                row=cur.fetchone()
                if row["order_id"]!=order_id or row["actor_user_id"]!=actor_id or row["note"]!=note:
                    raise ValueError("idempotency key was reused with different reconciliation data")
                return row
            _audit(conn,actor_id=actor_id,action="superadmin.payment.reconcile_note",
                   target_type="payment_order",target_id=str(order_id),
                   after={"note_id":row["id"],"observed_status":order["status"]})
            return row


def list_reconciliation_notes(conn, *, order_id: UUID, limit: int = 50):
    if not 1<=limit<=100:
        raise ValueError("limit must be between 1 and 100")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT n.id,n.order_id,n.actor_user_id,u.username,n.observed_status,n.note,n.created_at
               FROM payment_reconciliation_notes n JOIN users u ON u.id=n.actor_user_id
               WHERE n.order_id=%s ORDER BY n.created_at DESC LIMIT %s""",
            (order_id,limit),
        )
        return cur.fetchall()


def get_admin_wallet(conn, *, user_id: UUID):
    balances=get_wallet_balances(conn,user_id)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,bucket,entry_type,amount_toman,reference_type,reference_id,
                      description,created_at
               FROM wallet_ledger_entries WHERE user_id=%s
               ORDER BY created_at DESC,id DESC LIMIT 100""",(user_id,)
        )
        ledger=cur.fetchall()
        cur.execute("SELECT status,COUNT(*)::bigint AS total FROM withdrawal_requests WHERE user_id=%s GROUP BY status",(user_id,))
        withdrawals=cur.fetchall()
    return {"balances":{"cash_toman":balances.cash_toman,"gift_toman":balances.gift_toman,
                        "total_toman":balances.total_toman,"reserved_cash_toman":balances.reserved_cash_toman,
                        "withdrawable_toman":balances.withdrawable_toman},
            "ledger":ledger,"withdrawals_by_status":withdrawals}


def adjust_wallet(conn, *, actor_id: UUID, user_id: UUID, bucket: str,
                  amount_toman: int, reason: str, idempotency_key: str):
    if bucket not in {"cash","gift"}:
        raise ValueError("bucket must be cash or gift")
    if isinstance(amount_toman,bool) or not isinstance(amount_toman,int) or amount_toman==0:
        raise ValueError("amount_toman must be a non-zero integer")
    reason=reason.strip()
    if not 3<=len(reason)<=500:
        raise ValueError("reason must contain 3-500 characters")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM users WHERE id=%s FOR UPDATE",(user_id,))
            if not cur.fetchone():
                raise LookupError("user not found")
        _lock_wallet(conn,user_id)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(amount_toman),0)::bigint AS balance
                   FROM wallet_ledger_entries WHERE user_id=%s AND bucket=%s""",
                (user_id,bucket),
            )
            balance=cur.fetchone()["balance"]
            if balance+amount_toman<0:
                raise ValueError("adjustment cannot make the wallet bucket negative")
            entry=post_ledger_entry(
                conn,user_id=user_id,bucket=bucket,
                entry_type="admin_credit" if amount_toman>0 else "admin_debit",
                amount_toman=amount_toman,idempotency_key=idempotency_key,
                reference_type="admin_adjustment",reference_id=None,
                description=reason,
            )
            _audit(conn,actor_id=actor_id,action="superadmin.wallet.adjust",
                   target_type="user",target_id=str(user_id),
                   after={"ledger_entry_id":entry["id"],"bucket":bucket,
                          "amount_toman":amount_toman,"reason":reason})
            return entry


def list_withdrawals(conn, *, status: str | None, limit: int = 50, offset: int = 0):
    allowed={"requested","approved","rejected","paid","cancelled"}
    if status is not None and status not in allowed:
        raise ValueError("invalid withdrawal status")
    if not 1<=limit<=100 or not 0<=offset<=10_000:
        raise ValueError("invalid pagination")
    where="WHERE w.status=%s" if status else ""
    params=[status] if status else []
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT w.id,w.user_id,u.username,w.amount_toman,w.status,
                       w.requested_at,w.reviewed_at,w.reviewed_by,w.paid_at,w.review_note,
                       w.bank_account_id
                FROM withdrawal_requests w JOIN users u ON u.id=w.user_id
                {where} ORDER BY w.requested_at DESC LIMIT %s OFFSET %s""",
            (*params,limit,offset),
        )
        return cur.fetchall()


def review_withdrawal(conn, *, actor_id: UUID, withdrawal_id: UUID,
                      decision: str, note: str):
    if decision not in {"approved","rejected"}:
        raise ValueError("decision must be approved or rejected")
    note=note.strip()
    if not 3<=len(note)<=500:
        raise ValueError("review note must contain 3-500 characters")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id,status,amount_toman FROM withdrawal_requests WHERE id=%s FOR UPDATE",
                (withdrawal_id,),
            )
            before=cur.fetchone()
            if not before:
                raise LookupError("withdrawal not found")
            if before["status"]!="requested":
                raise ValueError("only requested withdrawals can be reviewed")
            cur.execute(
                """UPDATE withdrawal_requests SET status=%s,reviewed_at=now(),
                   reviewed_by=%s,review_note=%s WHERE id=%s
                   RETURNING id,user_id,amount_toman,status,reviewed_at,reviewed_by,review_note""",
                (decision,actor_id,note,withdrawal_id),
            )
            updated=cur.fetchone()
            _audit(conn,actor_id=actor_id,action="superadmin.withdrawal.review",
                   target_type="withdrawal",target_id=str(withdrawal_id),
                   before={"status":before["status"]},
                   after={"status":decision,"amount_toman":before["amount_toman"],"note":note})
            return updated


def list_referrals(conn, *, status: str | None, limit: int = 50, offset: int = 0):
    allowed={"pending","qualified","rewarded","rejected"}
    if status is not None and status not in allowed:
        raise ValueError("invalid referral status")
    if not 1<=limit<=100 or not 0<=offset<=10_000:
        raise ValueError("invalid pagination")
    where="WHERE r.status=%s" if status else ""
    params=[status] if status else []
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT r.id,r.status,r.created_at,r.qualified_at,
                       inviter.id AS inviter_user_id,inviter.username AS inviter_username,
                       referred.id AS referred_user_id,referred.username AS referred_username,
                       reward.amount_toman AS reward_toman,reward.ledger_entry_id,
                       reward.sms_status
                FROM referrals r
                JOIN users inviter ON inviter.id=r.inviter_user_id
                JOIN users referred ON referred.id=r.referred_user_id
                LEFT JOIN referral_rewards reward ON reward.referral_id=r.id
                {where} ORDER BY r.created_at DESC LIMIT %s OFFSET %s""",
            (*params,limit,offset),
        )
        return cur.fetchall()


def list_audit_logs(conn, *, actor_id: UUID | None, action: str | None,
                    target_type: str | None, limit: int = 50, offset: int = 0):
    if not 1<=limit<=100 or not 0<=offset<=10_000:
        raise ValueError("invalid pagination")
    clauses=[]
    params=[]
    if actor_id:
        clauses.append("a.actor_user_id=%s")
        params.append(actor_id)
    if action:
        clauses.append("a.action=%s")
        params.append(action)
    if target_type:
        clauses.append("a.target_type=%s")
        params.append(target_type)
    where=("WHERE "+" AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT a.id,a.actor_user_id,u.username AS actor_username,a.action,
                       a.target_type,a.target_id,a.request_id,a.before_state,a.after_state,a.created_at
                FROM admin_audit_logs a LEFT JOIN users u ON u.id=a.actor_user_id
                {where} ORDER BY a.created_at DESC,a.id DESC LIMIT %s OFFSET %s""",
            (*params,limit,offset),
        )
        return cur.fetchall()
