"""Permission-checked administrative console queries and audited mutations."""
from __future__ import annotations
from uuid import UUID
from saas_core.auth.authorization import require_permission
from saas_core.payments.repository import list_payment_reconciliation_cases, resolve_payment_reconciliation_case
from saas_core.wallet.wallet_repository import get_wallet_balances, list_wallet_ledger, post_ledger_entry
from saas_core.support.service import list_staff_tickets


def _audit(conn, actor_id, action, target_type, target_id, before=None, after=None):
    import json
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,before_state,after_state)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (actor_id,action,target_type,str(target_id),
             json.dumps(before,default=str) if before is not None else None,
             json.dumps(after,default=str) if after is not None else None),
        )


def console_overview(conn, *, actor_id: UUID):
    require_permission(conn,actor_id,"admin.console.access")
    with conn.cursor() as cur:
        cur.execute("""SELECT
          (SELECT count(*) FROM users)::int AS total_users,
          (SELECT count(*) FROM users WHERE status='active')::int AS active_users,
          (SELECT count(*) FROM subscriptions WHERE status='active' AND ends_at>now())::int AS active_subscriptions,
          (SELECT count(*) FROM payment_orders WHERE status IN ('created','pending'))::int AS pending_payments,
          (SELECT count(*) FROM payment_reconciliation_cases WHERE status='open')::int AS open_reconciliation_cases,
          (SELECT count(*) FROM withdrawal_requests WHERE status IN ('requested','approved'))::int AS pending_withdrawals,
          (SELECT count(*) FROM referrals WHERE status IN ('pending','qualified'))::int AS pending_referrals,
          (SELECT count(*) FROM tickets WHERE status IN ('open','waiting'))::int AS open_tickets,
          (SELECT count(*) FROM analytics_events WHERE received_at>=date_trunc('day',now()))::int AS events_today""")
        return cur.fetchone()


def list_users(conn, *, actor_id: UUID, query: str | None=None, status: str | None=None,
               limit: int=50, offset: int=0):
    require_permission(conn,actor_id,"users.read")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("invalid user pagination")
    if status is not None and status not in {"active","suspended","deleted","pending"}:
        raise ValueError("invalid user status")
    term=(query or "").strip()
    if len(term)>100:
        raise ValueError("user search query is too long")
    pattern=f"%{term}%"
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.id,u.public_id,u.username,u.display_name,u.status,u.account_type,
                      u.phone_verified_at IS NOT NULL AS phone_verified,
                      u.email_verified_at IS NOT NULL AS email_verified,
                      u.created_at,u.last_login_at,
                      COALESCE(array_agg(DISTINCT r.code) FILTER(WHERE r.code IS NOT NULL),'{}') AS roles
               FROM users u LEFT JOIN user_roles ur ON ur.user_id=u.id
               LEFT JOIN roles r ON r.id=ur.role_id
               WHERE (%s='' OR COALESCE(u.username,'') ILIKE %s OR
                      COALESCE(u.display_name,'') ILIKE %s OR u.public_id ILIKE %s)
                 AND (%s IS NULL OR u.status=%s)
               GROUP BY u.id ORDER BY u.created_at DESC,u.id LIMIT %s OFFSET %s""",
            (term,pattern,pattern,pattern,status,status,limit,offset),
        )
        return cur.fetchall()


def get_user_detail(conn, *, actor_id: UUID, user_id: UUID):
    require_permission(conn,actor_id,"users.read")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,public_id,username,display_name,status,account_type,
                      phone_e164,phone_verified_at,email,email_verified_at,created_at,last_login_at
               FROM users WHERE id=%s""",(user_id,))
        user=cur.fetchone()
        if not user:
            raise LookupError("user not found")
        cur.execute("""SELECT r.code,r.title,ur.granted_at FROM user_roles ur
                       JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=%s ORDER BY r.code""",(user_id,))
        roles=cur.fetchall()
        cur.execute("""SELECT s.id,s.status,s.starts_at,s.ends_at,p.code AS plan_code,p.title AS plan_title,
                              o.amount_toman,o.duration_days
                       FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                       LEFT JOIN payment_orders o ON o.id=s.payment_order_id
                       WHERE s.user_id=%s ORDER BY s.created_at DESC LIMIT 20""",(user_id,))
        subscriptions=cur.fetchall()
        return {"user":user,"roles":roles,"subscriptions":subscriptions}


def set_user_status(conn, *, actor_id: UUID, user_id: UUID, status: str, reason: str):
    require_permission(conn,actor_id,"users.manage")
    note=(reason or "").strip()
    if status not in {"active","suspended"} or not note or len(note)>500:
        raise ValueError("status must be active or suspended and a reason is required")
    if actor_id==user_id and status=="suspended":
        raise ValueError("administrators cannot suspend their own account")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id,status FROM users WHERE id=%s FOR UPDATE",(user_id,))
            before=cur.fetchone()
            if not before:
                raise LookupError("user not found")
            if before["status"]==status:
                return {"id":user_id,"status":status,"changed":False}
            cur.execute("""SELECT EXISTS(
                SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                WHERE ur.user_id=%s AND r.code='superadmin') AS is_superadmin""",(user_id,))
            target_is_superadmin=cur.fetchone()["is_superadmin"]
            if target_is_superadmin and status=="suspended":
                cur.execute("""SELECT count(DISTINCT u.id)::int AS n FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id JOIN roles r ON r.id=ur.role_id
                    WHERE r.code='superadmin' AND u.status='active'""")
                if cur.fetchone()["n"]<=1:
                    raise ValueError("cannot suspend the last active superadmin")
                raise ValueError("superadmin accounts must be managed out of band")
            cur.execute("UPDATE users SET status=%s,updated_at=now() WHERE id=%s RETURNING id,status",(status,user_id))
            result=cur.fetchone()
            if status=="suspended":
                cur.execute("UPDATE sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",(user_id,))
            _audit(conn,actor_id,"admin.user.status_changed","user",user_id,
                   {"status":before["status"]},{"status":status,"reason":note})
            return {**result,"changed":True}


def list_roles(conn, *, actor_id: UUID):
    require_permission(conn,actor_id,"roles.manage")
    with conn.cursor() as cur:
        cur.execute("""SELECT r.code,r.title,
                       COALESCE(array_agg(p.code ORDER BY p.code) FILTER(WHERE p.code IS NOT NULL),'{}') AS permissions
                       FROM roles r LEFT JOIN role_permissions rp ON rp.role_id=r.id
                       LEFT JOIN permissions p ON p.id=rp.permission_id
                       GROUP BY r.id ORDER BY r.code""")
        return cur.fetchall()


def change_user_role(conn, *, actor_id: UUID, user_id: UUID, role_code: str, grant: bool):
    require_permission(conn,actor_id,"roles.manage")
    if role_code not in {"user","support","admin"}:
        raise ValueError("superadmin role is provisioned out of band")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE",(user_id,))
            if not cur.fetchone():
                raise LookupError("user not found")
            cur.execute("SELECT id FROM roles WHERE code=%s",(role_code,))
            role=cur.fetchone()
            if not role:
                raise LookupError("role not found")
            if grant:
                cur.execute("""INSERT INTO user_roles(user_id,role_id,granted_by)
                               VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING user_id""",
                            (user_id,role["id"],actor_id))
                changed=cur.fetchone() is not None
            else:
                cur.execute("DELETE FROM user_roles WHERE user_id=%s AND role_id=%s RETURNING user_id",
                            (user_id,role["id"]))
                changed=cur.fetchone() is not None
            _audit(conn,actor_id,"admin.user.role_granted" if grant else "admin.user.role_revoked",
                   "user",user_id,after={"role":role_code,"changed":changed})
            return {"user_id":user_id,"role":role_code,"granted":grant,"changed":changed}


def list_subscriptions(conn, *, actor_id: UUID, status: str | None=None, limit: int=100, offset: int=0):
    require_permission(conn,actor_id,"subscriptions.manage")
    if not 1 <= limit <= 200 or offset<0:
        raise ValueError("invalid subscription pagination")
    if status is not None and status not in {"pending","active","expired","cancelled","refunded"}:
        raise ValueError("invalid subscription status")
    with conn.cursor() as cur:
        cur.execute("""SELECT s.id,s.user_id,u.username,s.status,s.starts_at,s.ends_at,s.created_at,
                              p.code AS plan_code,p.title AS plan_title,o.amount_toman,o.duration_days
                       FROM subscriptions s JOIN users u ON u.id=s.user_id
                       JOIN plans p ON p.id=s.plan_id LEFT JOIN payment_orders o ON o.id=s.payment_order_id
                       WHERE (%s IS NULL OR s.status=%s)
                       ORDER BY s.created_at DESC LIMIT %s OFFSET %s""",
                    (status,status,limit,offset))
        return cur.fetchall()


def list_payment_orders(conn, *, actor_id: UUID, status: str | None=None, limit: int=100, offset: int=0):
    require_permission(conn,actor_id,"payments.read")
    if not 1 <= limit <= 200 or offset<0:
        raise ValueError("invalid payment pagination")
    if status is not None and status not in {"created","pending","paid","failed","cancelled","refunded"}:
        raise ValueError("invalid payment status")
    with conn.cursor() as cur:
        cur.execute("""SELECT o.id,o.user_id,u.username,o.plan_id,p.code AS plan_code,
                              o.amount_toman,o.duration_days,o.currency,o.status,o.created_at,o.paid_at,o.expires_at,
                              a.provider,a.status AS attempt_status,a.provider_order_id,a.request_ref
                       FROM payment_orders o JOIN users u ON u.id=o.user_id
                       LEFT JOIN plans p ON p.id=o.plan_id
                       LEFT JOIN LATERAL (SELECT provider,status,provider_order_id,request_ref
                                          FROM payment_attempts WHERE order_id=o.id
                                          ORDER BY created_at DESC LIMIT 1) a ON TRUE
                       WHERE (%s IS NULL OR o.status=%s)
                       ORDER BY o.created_at DESC LIMIT %s OFFSET %s""",
                    (status,status,limit,offset))
        return cur.fetchall()


def list_reconciliation(conn, *, actor_id: UUID, limit: int=100):
    require_permission(conn,actor_id,"payments.read")
    return list_payment_reconciliation_cases(conn,limit=limit)


def resolve_reconciliation(conn, *, actor_id: UUID, case_id: UUID, note: str, dismissed: bool=False):
    require_permission(conn,actor_id,"payments.read")
    return resolve_payment_reconciliation_case(conn,case_id=case_id,resolved_by=actor_id,
                                                resolution_note=note,dismissed=dismissed)


def wallet_detail(conn, *, actor_id: UUID, user_id: UUID, limit: int=50):
    require_permission(conn,actor_id,"wallet.read")
    if not 1 <= limit <= 100:
        raise ValueError("invalid wallet ledger limit")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id=%s",(user_id,))
        if not cur.fetchone():
            raise LookupError("user not found")
    balances=get_wallet_balances(conn,user_id)
    return {"user_id":user_id,"cash_toman":balances.cash_toman,"gift_toman":balances.gift_toman,
            "reserved_cash_toman":balances.reserved_cash_toman,"withdrawable_toman":balances.withdrawable_toman,
            "entries":list_wallet_ledger(conn,user_id,limit=limit)}


def adjust_wallet(conn, *, actor_id: UUID, user_id: UUID, bucket: str, amount_toman: int,
                  reason: str, idempotency_key: str):
    require_permission(conn,actor_id,"wallet.adjust")
    note=(reason or "").strip()
    if bucket not in {"cash","gift"} or isinstance(amount_toman,bool) or not isinstance(amount_toman,int) or amount_toman==0:
        raise ValueError("wallet adjustment requires a bucket and non-zero integer Toman amount")
    if not note or len(note)>500 or not idempotency_key or len(idempotency_key)>120:
        raise ValueError("reason and idempotency key are required")
    entry_type="admin_credit" if amount_toman>0 else "admin_debit"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id=%s",(user_id,))
            if not cur.fetchone():
                raise LookupError("user not found")
        entry=post_ledger_entry(conn,user_id=user_id,bucket=bucket,entry_type=entry_type,
            amount_toman=amount_toman,idempotency_key=idempotency_key,
            reference_type="admin_adjustment",description=note)
        _audit(conn,actor_id,"admin.wallet.adjusted","user",user_id,
               after={"entry_id":entry["id"],"bucket":bucket,"amount_toman":amount_toman,"reason":note})
        return entry


def list_withdrawals(conn, *, actor_id: UUID, status: str | None=None, limit: int=100, offset: int=0):
    require_permission(conn,actor_id,"withdrawals.review")
    if not 1 <= limit <= 200 or offset<0:
        raise ValueError("invalid withdrawal pagination")
    if status is not None and status not in {"requested","approved","rejected","paid","cancelled"}:
        raise ValueError("invalid withdrawal status")
    with conn.cursor() as cur:
        cur.execute("""SELECT w.id,w.user_id,u.username,w.amount_toman,w.status,w.requested_at,
                              w.reviewed_at,w.reviewed_by,w.review_note,w.payout_reference,
                              b.bank_name,b.verification_status
                       FROM withdrawal_requests w JOIN users u ON u.id=w.user_id
                       LEFT JOIN wallet_bank_accounts b ON b.id=w.bank_account_id
                       WHERE (%s IS NULL OR w.status=%s)
                       ORDER BY w.requested_at DESC LIMIT %s OFFSET %s""",
                    (status,status,limit,offset))
        return cur.fetchall()


def list_referrals(conn, *, actor_id: UUID, status: str | None=None, limit: int=100, offset: int=0):
    require_permission(conn,actor_id,"referrals.read")
    if not 1 <= limit <= 200 or offset<0:
        raise ValueError("invalid referral pagination")
    if status is not None and status not in {"pending","qualified","rewarded","rejected"}:
        raise ValueError("invalid referral status")
    with conn.cursor() as cur:
        cur.execute("""SELECT r.id,r.status,r.created_at,r.qualified_at,
                              i.id AS inviter_user_id,i.username AS inviter_username,
                              u.id AS referred_user_id,u.username AS referred_username,
                              rr.amount_toman AS reward_amount_toman,rr.sms_status
                       FROM referrals r JOIN users i ON i.id=r.inviter_user_id
                       JOIN users u ON u.id=r.referred_user_id
                       LEFT JOIN referral_rewards rr ON rr.referral_id=r.id
                       WHERE (%s IS NULL OR r.status=%s)
                       ORDER BY r.created_at DESC LIMIT %s OFFSET %s""",
                    (status,status,limit,offset))
        return cur.fetchall()


def list_audit_logs(conn, *, actor_id: UUID, action: str | None=None, limit: int=100, offset: int=0):
    require_permission(conn,actor_id,"audit.read")
    if not 1 <= limit <= 200 or offset<0:
        raise ValueError("invalid audit pagination")
    if action is not None and (not action or len(action)>100):
        raise ValueError("invalid audit action filter")
    with conn.cursor() as cur:
        cur.execute("""SELECT l.id,l.actor_user_id,u.username AS actor_username,l.action,
                              l.target_type,l.target_id,l.request_id,l.before_state,l.after_state,l.created_at
                       FROM admin_audit_logs l LEFT JOIN users u ON u.id=l.actor_user_id
                       WHERE (%s IS NULL OR l.action=%s)
                       ORDER BY l.created_at DESC,l.id DESC LIMIT %s OFFSET %s""",
                    (action,action,limit,offset))
        return cur.fetchall()


def list_support_queue(conn, *, actor_id: UUID, status: str | None=None, limit: int=100, offset: int=0):
    return list_staff_tickets(conn,actor_id=actor_id,status=status,limit=limit,offset=offset)
