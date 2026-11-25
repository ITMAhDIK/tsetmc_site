"""Payment persistence. All operations use the isolated SaaS PostgreSQL connection."""
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
                   (user_id,plan_id,amount_toman,status,idempotency_key,phone_e164,expires_at)
                   VALUES (%s,%s,%s,'created',%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING id,user_id,plan_id,amount_toman,status,created_at,expires_at""",
                (user_id, plan["id"], plan["price_toman"], idempotency_key,
                 user["phone_e164"], datetime.now(timezone.utc) + ORDER_TTL),
            )
            order = cur.fetchone()
            if order is None:
                cur.execute(
                    """SELECT id,user_id,plan_id,amount_toman,status,created_at,expires_at
                       FROM payment_orders WHERE idempotency_key=%s""", (idempotency_key,)
                )
                order = cur.fetchone()
                if order["user_id"] != user_id or order["plan_id"] != plan["id"]:
                    raise ValueError("idempotency key was already used for a different order")
            order["plan_code"] = plan["code"]
            order["plan_title"] = plan["title"]
            order["duration_days"] = plan["duration_days"]
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
                # Do not mutate and then raise inside this transaction: the raise
                # would roll the cancellation back. Expiry is handled by a worker. 
                raise ValueError("order has expired")
            cur.execute(
                """INSERT INTO payment_attempts(order_id,provider,amount_toman,currency,status)
                   VALUES (%s,%s,%s,'toman','initiated')
                   RETURNING id,order_id,provider,amount_toman,status""",
                (order_id,provider,order["amount_toman"]),
            )
            return cur.fetchone()

def attach_provider_checkout(conn, *, attempt_id: UUID, provider_order_id: str,
                             redirect_url: str, request_ref: str | None):
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
    """Atomically deduplicate provider event, mark paid and activate one subscription."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.id,o.user_id,o.plan_id,o.amount_toman,o.status,
                          p.duration_days,a.provider,a.provider_order_id,a.status AS attempt_status
                   FROM payment_orders o
                   JOIN plans p ON p.id=o.plan_id
                   JOIN payment_attempts a ON a.order_id=o.id
                   WHERE o.id=%s AND a.id=%s FOR UPDATE OF o,a""",
                (order_id,attempt_id),
            )
            row = cur.fetchone()
            if not row or row["provider"] != provider:
                raise LookupError("payment attempt not found")
            if row["provider_order_id"] != verified.provider_order_id:
                raise PaymentRejected("provider order reference mismatch")
            validate_verified_payment(expected_amount_toman=row["amount_toman"],verified=verified)
            cur.execute(
                """INSERT INTO gateway_events
                   (provider,provider_event_id,order_id,event_type,payload_sha256,processing_status)
                   VALUES (%s,%s,%s,'payment.verified',%s,'received')
                   ON CONFLICT (provider,provider_event_id) DO NOTHING
                   RETURNING id""",
                (provider,verified.provider_event_id,order_id,payload_sha256),
            )
            event = cur.fetchone()
            if not event:
                cur.execute(
                    "SELECT processing_status FROM gateway_events WHERE provider=%s AND provider_event_id=%s",
                    (provider,verified.provider_event_id),
                )
                duplicate = cur.fetchone()
                if duplicate and duplicate["processing_status"] == "processed":
                    return {"already_processed": True, "order_id": order_id}
                raise PaymentRejected("provider event was already received and needs reconciliation")
            if row["status"] == "paid":
                cur.execute(
                    "UPDATE gateway_events SET processing_status='processed',processed_at=now() WHERE id=%s",
                    (event["id"],),
                )
                return {"already_processed": True, "order_id": order_id}
            if row["status"] not in {"created","pending"}:
                raise PaymentRejected("order is not eligible for settlement")
            cur.execute(
                """UPDATE payment_attempts SET status='verified',verified_at=now()
                   WHERE id=%s""", (attempt_id,)
            )
            cur.execute(
                """UPDATE payment_orders SET status='paid',paid_at=now(),updated_at=now()
                   WHERE id=%s""", (order_id,)
            )
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
            cur.execute(
                "UPDATE gateway_events SET processing_status='processed',processed_at=now() WHERE id=%s",
                (event["id"],),
            )
            return {"already_processed": False, "order_id": order_id}
