"""Payment persistence. All operations use the dedicated Commerce PostgreSQL connection."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from uuid import UUID
from .domain import PaymentRejected, validate_verified_payment

ORDER_TTL = timedelta(minutes=20)

def create_subscription_order(conn, *, user_id: UUID, plan_code: str, idempotency_key: str):
    if not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("a valid idempotency key is required")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT phone_e164, phone_verified_at, status FROM users WHERE id=%s FOR UPDATE",
                (user_id,),
            )
            user = cur.fetchone()
            if not user or user["status"] != "active":
                raise LookupError("active account not found")
            if not user["phone_e164"] or not user["phone_verified_at"]:
                raise PermissionError("verified phone is required before purchase")
            cur.execute(
                "SELECT id, code, title, duration_days, price_toman FROM plans "
                "WHERE code=%s AND active=TRUE FOR SHARE",
                (plan_code,),
            )
            plan = cur.fetchone()
            if not plan:
                raise LookupError("active subscription plan not found")
            cur.execute(
                """INSERT INTO payment_orders
                   (user_id,plan_id,amount_toman,plan_duration_days,status,idempotency_key,phone_e164,expires_at)
                   VALUES (%s,%s,%s,%s,'created',%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING id,user_id,plan_id,amount_toman,plan_duration_days,status,created_at,expires_at""",
                (user_id, plan["id"], plan["price_toman"], plan["duration_days"], idempotency_key,
                 user["phone_e164"], datetime.now(timezone.utc) + ORDER_TTL),
            )
            order = cur.fetchone()
            if order is None:
                cur.execute(
                    """SELECT id,user_id,plan_id,amount_toman,plan_duration_days,status,created_at,expires_at
                       FROM payment_orders WHERE idempotency_key=%s""", (idempotency_key,)
                )
                order = cur.fetchone()
                if order["user_id"] != user_id or order["plan_id"] != plan["id"]:
                    raise ValueError("idempotency key was already used for a different order")
            order["plan_code"] = plan["code"]
            order["plan_title"] = plan["title"]
            order["duration_days"] = order["plan_duration_days"]
            return order

def get_order_for_user(conn, *, order_id: UUID, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT o.id,o.user_id,o.plan_id,o.amount_toman,o.status,o.created_at,
                      o.expires_at,o.paid_at,p.code AS plan_code,p.title AS plan_title
               FROM payment_orders o JOIN plans p ON p.id=o.plan_id
               WHERE o.id=%s AND o.user_id=%s""", (order_id,user_id)
        )
        return cur.fetchone()

def create_payment_attempt(conn, *, order_id: UUID, provider: str):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,amount_toman,status,expires_at FROM payment_orders
                   WHERE id=%s FOR UPDATE""", (order_id,)
            )
            order = cur.fetchone()
            if not order:
                raise LookupError("order not found")
            if order["status"] not in {"created","pending"}:
                raise ValueError("order is not payable")
            if order["expires_at"] and order["expires_at"] <= datetime.now(timezone.utc):
                raise ValueError("order has expired")
            cur.execute(
                """SELECT id FROM payment_attempts
                   WHERE order_id=%s AND status IN ('initiated','pending')
                   LIMIT 1 FOR UPDATE""", (order_id,)
            )
            if cur.fetchone():
                raise ValueError("an active checkout already exists for this order")
            cur.execute(
                """INSERT INTO payment_attempts(order_id,provider,amount_toman,currency,status)
                   VALUES (%s,%s,%s,'toman','initiated')
                   RETURNING id,order_id,provider,amount_toman,status""",
                (order_id,provider,order["amount_toman"]),
            )
            return cur.fetchone()

def attach_provider_checkout(conn, *, attempt_id: UUID, provider_order_id: str,
                             redirect_url: str, request_ref: str | None):
    if not provider_order_id or len(provider_order_id) > 160:
        raise ValueError("invalid provider order reference")
    if not redirect_url.startswith("https://"):
        raise ValueError("gateway checkout URL must use HTTPS")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE payment_attempts
                   SET provider_order_id=%s,redirect_url=%s,request_ref=%s,status='pending'
                   WHERE id=%s AND status='initiated'
                   RETURNING order_id""",
                (provider_order_id,redirect_url,request_ref,attempt_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("payment attempt is no longer initiatable")
            cur.execute(
                "UPDATE payment_orders SET status='pending',updated_at=now() WHERE id=%s",
                (row["order_id"],),
            )

def mark_attempt_failed(conn, *, attempt_id: UUID, failure_code: str, failure_message: str):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE payment_attempts SET status='failed',failure_code=%s,
                   failure_message=%s WHERE id=%s AND status='initiated'""",
                (failure_code[:80],failure_message[:300],attempt_id),
            )

def settle_verified_payment(conn, *, order_id: UUID, attempt_id: UUID, provider: str,
                            verified, payload_sha256: str):
    """Settle once; accept only an exact replay of the already-processed provider event."""
    import re

    if not isinstance(payload_sha256,str) or not re.fullmatch(r"[0-9a-fA-F]{64}",payload_sha256):
        raise ValueError("payload_sha256 must be a 64-character SHA-256 hex digest")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.id,o.user_id,o.plan_id,o.amount_toman,o.plan_duration_days AS duration_days,o.status,
                          a.provider,a.provider_order_id,a.amount_toman AS attempt_amount_toman,
                          a.status AS attempt_status
                   FROM payment_orders o
                   JOIN payment_attempts a ON a.order_id=o.id
                   WHERE o.id=%s AND a.id=%s FOR UPDATE OF o,a""",
                (order_id,attempt_id),
            )
            row = cur.fetchone()
            if not row or row["provider"] != provider:
                raise LookupError("payment attempt not found")
            if row["provider_order_id"] != verified.provider_order_id:
                raise PaymentRejected("provider order reference mismatch")

            # A retry is successful only when it is byte-for-byte bound to the
            # same provider event, order and payload digest. Reusing an event ID
            # with different content is a hard rejection, not a silent dedupe.
            cur.execute(
                """SELECT order_id,event_type,payload_sha256,processing_status
                   FROM gateway_events WHERE provider=%s AND provider_event_id=%s""",
                (provider,verified.provider_event_id),
            )
            prior_event = cur.fetchone()
            if prior_event:
                same_event = (
                    prior_event["order_id"] == order_id
                    and prior_event["event_type"] == "payment.verified"
                    and prior_event["payload_sha256"].strip() == payload_sha256
                )
                if not same_event:
                    raise PaymentRejected("provider event ID was reused with different event data")
                if prior_event["processing_status"] == "processed":
                    return {"already_processed":True,"order_id":order_id}
                raise PaymentRejected("provider event was already received and needs reconciliation")

            if row["status"] == "paid":
                raise PaymentRejected("order is already settled by a different provider event")
            if row["status"] not in {"created","pending"}:
                raise PaymentRejected("order is not eligible for settlement")
            if row["attempt_status"] != "pending":
                raise PaymentRejected("payment attempt is not awaiting verification")
            if row["attempt_amount_toman"] != row["amount_toman"]:
                raise PaymentRejected("payment attempt amount does not match the order")

            validate_verified_payment(expected_amount_toman=row["amount_toman"],verified=verified)
            cur.execute(
                """INSERT INTO gateway_events
                   (provider,provider_event_id,order_id,event_type,payload_sha256,processing_status)
                   VALUES (%s,%s,%s,'payment.verified',%s,'received')
                   ON CONFLICT (provider,provider_event_id) DO NOTHING RETURNING id""",
                (provider,verified.provider_event_id,order_id,payload_sha256),
            )
            event = cur.fetchone()
            if not event:
                # This can occur when the same provider event is concurrently
                # delivered for another order. Re-read and verify the exact tuple.
                cur.execute(
                    """SELECT order_id,event_type,payload_sha256,processing_status
                       FROM gateway_events WHERE provider=%s AND provider_event_id=%s""",
                    (provider,verified.provider_event_id),
                )
                duplicate = cur.fetchone()
                if (duplicate and duplicate["order_id"] == order_id
                        and duplicate["event_type"] == "payment.verified"
                        and duplicate["payload_sha256"].strip() == payload_sha256
                        and duplicate["processing_status"] == "processed"):
                    return {"already_processed":True,"order_id":order_id}
                raise PaymentRejected("provider event was already received and needs reconciliation")

            cur.execute(
                "UPDATE payment_attempts SET status='verified',verified_at=now(),settled_at=now() WHERE id=%s",
                (attempt_id,),
            )
            cur.execute(
                "UPDATE payment_orders SET status='paid',paid_at=now(),updated_at=now() WHERE id=%s",
                (order_id,),
            )
            cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (row["user_id"],))
            cur.execute(
                """WITH next_start AS (
                       SELECT GREATEST(now(), COALESCE(MAX(ends_at), now())) AS starts_at
                       FROM subscriptions
                       WHERE user_id=%s AND status='active' AND ends_at>now()
                   )
                   INSERT INTO subscriptions
                       (user_id,plan_id,status,starts_at,ends_at,payment_order_id)
                   SELECT %s,%s,'active',next_start.starts_at,
                          next_start.starts_at + (%s * interval '1 day'),%s
                   FROM next_start
                   ON CONFLICT (payment_order_id) WHERE payment_order_id IS NOT NULL DO NOTHING""",
                (row["user_id"],row["user_id"],row["plan_id"],row["duration_days"],order_id),
            )
            # Referral rewards are issued only from this trusted, verified settlement path.
            from commerce_service.wallet.referrals import qualify_and_reward_referral
            qualify_and_reward_referral(
                conn,referred_user_id=row["user_id"],payment_order_id=order_id
            )
            cur.execute(
                "UPDATE gateway_events SET processing_status='processed',processed_at=now() WHERE id=%s",
                (event["id"],),
            )
            return {"already_processed":False,"order_id":order_id}


def find_attempt_by_provider_order(conn, *, provider: str, provider_order_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id AS attempt_id,a.order_id,a.provider,a.provider_order_id,
                      o.amount_toman,o.status AS order_status
               FROM payment_attempts a JOIN payment_orders o ON o.id=a.order_id
               WHERE a.provider=%s AND a.provider_order_id=%s""",
            (provider,provider_order_id),
        )
        return cur.fetchone()



def pay_order_with_wallet(conn, *, order_id: UUID, user_id: UUID, idempotency_key: str):
    """Atomically debit gift then available cash and activate the paid subscription.

    An order with any unreconciled gateway attempt cannot be wallet-paid. The
    order row and wallet account are locked so concurrent gateway callbacks,
    wallet payments and withdrawals cannot spend or settle the same funds twice.
    """
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 120:
        raise ValueError("a valid wallet payment idempotency key is required")

    from commerce_service.wallet.wallet_repository import (
        _lock_wallet, get_wallet_balances, post_ledger_entry,
    )
    from commerce_service.wallet.referrals import qualify_and_reward_referral

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.id,o.user_id,o.plan_id,o.amount_toman,o.plan_duration_days AS duration_days,
                          o.status,o.expires_at
                   FROM payment_orders o
                   WHERE o.id=%s AND o.user_id=%s
                   FOR UPDATE OF o""",
                (order_id, user_id),
            )
            order = cur.fetchone()
            if not order:
                raise LookupError("order not found")

            cur.execute(
                """SELECT id,order_id,user_id,amount_toman,gift_debit_toman,
                          cash_debit_toman,idempotency_key
                   FROM wallet_order_payments
                   WHERE idempotency_key=%s
                   FOR UPDATE""",
                (idempotency_key,),
            )
            by_key = cur.fetchone()
            if by_key:
                if by_key["order_id"] != order_id or by_key["user_id"] != user_id:
                    raise ValueError("idempotency key was already used for a different wallet payment")
                return {
                    "order_id": order_id,
                    "status": "paid",
                    "amount_toman": by_key["amount_toman"],
                    "gift_debit_toman": by_key["gift_debit_toman"],
                    "cash_debit_toman": by_key["cash_debit_toman"],
                    "already_processed": True,
                }

            cur.execute(
                "SELECT id FROM wallet_order_payments WHERE order_id=%s FOR UPDATE",
                (order_id,),
            )
            if cur.fetchone():
                raise ValueError("order has already been paid from the wallet")
            if order["status"] != "created":
                raise ValueError("only an unstarted order can be paid from the wallet")
            if order["expires_at"] and order["expires_at"] <= datetime.now(timezone.utc):
                raise ValueError("order has expired")

            # Even a failed initiation can have an unknown provider outcome
            # (for example, a network timeout after the provider accepted it).
            # Do not switch payment methods until that attempt is reconciled.
            cur.execute(
                """SELECT id FROM payment_attempts
                   WHERE order_id=%s
                   LIMIT 1 FOR UPDATE""",
                (order_id,),
            )
            if cur.fetchone():
                raise ValueError("order has a prior payment attempt that must be reconciled")

            cur.execute(
                """SELECT status,phone_e164,phone_verified_at
                   FROM users WHERE id=%s FOR UPDATE""",
                (user_id,),
            )
            user = cur.fetchone()
            if not user or user["status"] != "active":
                raise PermissionError("active account is required")
            if not user["phone_e164"] or not user["phone_verified_at"]:
                raise PermissionError("verified phone is required before purchase")

            _lock_wallet(conn, user_id)
            balances = get_wallet_balances(conn, user_id)
            spendable_cash = balances.withdrawable_toman
            spendable_total = balances.gift_toman + spendable_cash
            amount = order["amount_toman"]
            if amount > spendable_total:
                raise ValueError("insufficient spendable wallet balance")

            gift_debit = min(balances.gift_toman, amount)
            cash_debit = amount - gift_debit
            gift_entry = None
            cash_entry = None
            if gift_debit:
                gift_entry = post_ledger_entry(
                    conn, user_id=user_id, bucket="gift",
                    entry_type="purchase_debit", amount_toman=-gift_debit,
                    idempotency_key=f"wallet-order:{order_id}:gift",
                    reference_type="payment_order", reference_id=order_id,
                    description="Subscription paid from gift wallet balance",
                )
            if cash_debit:
                cash_entry = post_ledger_entry(
                    conn, user_id=user_id, bucket="cash",
                    entry_type="purchase_debit", amount_toman=-cash_debit,
                    idempotency_key=f"wallet-order:{order_id}:cash",
                    reference_type="payment_order", reference_id=order_id,
                    description="Subscription paid from cash wallet balance",
                )

            cur.execute(
                """INSERT INTO wallet_order_payments
                   (order_id,user_id,amount_toman,gift_debit_toman,cash_debit_toman,
                    gift_ledger_entry_id,cash_ledger_entry_id,idempotency_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    order_id, user_id, amount, gift_debit, cash_debit,
                    gift_entry["id"] if gift_entry else None,
                    cash_entry["id"] if cash_entry else None,
                    idempotency_key,
                ),
            )
            cur.execute(
                """UPDATE payment_orders SET status='paid',paid_at=now(),updated_at=now()
                   WHERE id=%s AND status='created'""",
                (order_id,),
            )
            if cur.rowcount != 1:
                raise ValueError("order changed while wallet payment was being processed")

            cur.execute(
                """WITH next_start AS (
                       SELECT GREATEST(now(),COALESCE(MAX(ends_at),now())) AS starts_at
                       FROM subscriptions
                       WHERE user_id=%s AND status='active' AND ends_at>now()
                   )
                   INSERT INTO subscriptions
                       (user_id,plan_id,status,starts_at,ends_at,payment_order_id)
                   SELECT %s,%s,'active',next_start.starts_at,
                          next_start.starts_at+(%s*interval '1 day'),%s
                   FROM next_start
                   ON CONFLICT (payment_order_id) WHERE payment_order_id IS NOT NULL
                   DO NOTHING""",
                (user_id,user_id,order["plan_id"],order["duration_days"],order_id),
            )
            qualify_and_reward_referral(
                conn, referred_user_id=user_id, payment_order_id=order_id,
            )
            return {
                "order_id": order_id,
                "status": "paid",
                "amount_toman": amount,
                "gift_debit_toman": gift_debit,
                "cash_debit_toman": cash_debit,
                "already_processed": False,
            }
