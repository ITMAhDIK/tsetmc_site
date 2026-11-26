"""Referral-code, qualification and reward policy.

Qualification is server-side only. A referred account must have a verified phone
and a first settled subscription payment. Rewards are credited to the gift bucket.
"""
from uuid import UUID
from .wallet_repository import _lock_wallet, post_ledger_entry

DEFAULT_REFERRAL_REWARD_TOMAN = 100_000


def ensure_referral_code(conn, user_id: UUID):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO referral_codes(user_id,code)
                   VALUES (%s,upper(substr(encode(gen_random_bytes(12),'hex'),1,16)))
                   ON CONFLICT(user_id) DO NOTHING""", (user_id,)
            )
            cur.execute("SELECT code FROM referral_codes WHERE user_id=%s",(user_id,))
            return cur.fetchone()["code"]


def attach_referral(conn, *, referred_user_id: UUID, code: str):
    normalized = code.strip().upper()
    if not normalized or len(normalized) > 24:
        raise ValueError("invalid referral code")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT rc.user_id FROM referral_codes rc
                   JOIN users u ON u.id=rc.user_id
                   WHERE rc.code=%s AND u.status='active'""",(normalized,)
            )
            inviter = cur.fetchone()
            if not inviter:
                raise LookupError("referral code not found")
            if inviter["user_id"] == referred_user_id:
                raise ValueError("self-referral is not allowed")
            cur.execute(
                """SELECT phone_e164,phone_verified_at,status FROM users
                   WHERE id=%s FOR UPDATE""",(referred_user_id,)
            )
            referred = cur.fetchone()
            if not referred or referred["status"] != "active":
                raise LookupError("active referred account not found")
            if not referred["phone_e164"] or not referred["phone_verified_at"]:
                raise PermissionError("verified phone is required for referral")
            cur.execute(
                """INSERT INTO referrals(inviter_user_id,referred_user_id,status)
                   VALUES (%s,%s,'pending')
                   ON CONFLICT (referred_user_id) DO NOTHING
                   RETURNING id,status""",
                (inviter["user_id"],referred_user_id),
            )
            row=cur.fetchone()
            if not row:
                raise ValueError("a referral is already attached to this account")
            return row


def qualify_and_reward_referral(conn, *, referred_user_id: UUID,
                                payment_order_id: UUID,
                                reward_toman: int | None = None):
    """Called only by trusted settlement code after first paid subscription."""
    with conn.transaction():
        with conn.cursor() as cur:
            if reward_toman is None:
                cur.execute("SELECT amount_toman FROM referral_reward_policies WHERE active=TRUE")
                policy=cur.fetchone()
                reward_toman=policy["amount_toman"] if policy else DEFAULT_REFERRAL_REWARD_TOMAN
            if isinstance(reward_toman,bool) or not isinstance(reward_toman,int) or reward_toman <= 0:
                raise ValueError("referral reward must be positive integer Toman")
            cur.execute(
                """SELECT r.id,r.inviter_user_id,r.status,u.phone_verified_at,
                          o.status AS order_status,s.id AS subscription_id
                   FROM referrals r
                   JOIN users u ON u.id=r.referred_user_id
                   JOIN payment_orders o ON o.id=%s AND o.user_id=r.referred_user_id
                   JOIN subscriptions s ON s.payment_order_id=o.id
                   WHERE r.referred_user_id=%s
                     AND (SELECT COUNT(*) FROM payment_orders paid_orders
                          WHERE paid_orders.user_id=r.referred_user_id
                            AND paid_orders.status='paid')=1
                   FOR UPDATE OF r""",(payment_order_id,referred_user_id)
            )
            row=cur.fetchone()
            if not row or row["order_status"] != "paid" or not row["phone_verified_at"]:
                return {"qualified":False,"reason":"qualifying payment not found"}
            if row["status"] in {"rewarded","rejected"}:
                return {"qualified":row["status"]=="rewarded","already_processed":True}
            _lock_wallet(conn,row["inviter_user_id"])
            cur.execute(
                """UPDATE referrals SET status='qualified',qualified_at=COALESCE(qualified_at,now())
                   WHERE id=%s AND status='pending'""",(row["id"],)
            )
            cur.execute(
                """INSERT INTO referral_rewards(referral_id,user_id,amount_toman)
                   VALUES (%s,%s,%s) ON CONFLICT(referral_id) DO NOTHING
                   RETURNING id,amount_toman""",(row["id"],row["inviter_user_id"],reward_toman)
            )
            reward=cur.fetchone()
            if reward:
                entry=post_ledger_entry(
                    conn,user_id=row["inviter_user_id"],bucket="gift",
                    entry_type="referral_reward",amount_toman=reward["amount_toman"],
                    idempotency_key=f"referral-reward:{row['id']}",
                    reference_type="referral",reference_id=row["id"],
                    description="Referral qualification reward",
                )
                cur.execute(
                    "UPDATE referral_rewards SET ledger_entry_id=%s WHERE id=%s",
                    (entry["id"],reward["id"])
                )
            cur.execute("UPDATE referrals SET status='rewarded' WHERE id=%s",(row["id"],))
            return {"qualified":True,"reward_toman":reward_toman}


def referral_summary(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 COUNT(*)::integer AS total,
                 COUNT(*) FILTER (WHERE status='pending')::integer AS pending,
                 COUNT(*) FILTER (WHERE status='qualified')::integer AS qualified,
                 COUNT(*) FILTER (WHERE status='rewarded')::integer AS rewarded
               FROM referrals WHERE inviter_user_id=%s""",(user_id,)
        )
        return cur.fetchone()
