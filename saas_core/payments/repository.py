"""Payment persistence. All operations use the isolated SaaS PostgreSQL connection."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from uuid import UUID
from .domain import PaymentRejected, validate_verified_payment
from saas_core.auth.authorization import require_permission

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
            if isinstance(plan["price_toman"], bool) or plan["price_toman"] <= 0:
                raise ValueError("subscription plan must have a positive payable price")
            cur.execute(
                """INSERT INTO payment_orders
                   (user_id,plan_id,amount_toman,duration_days,status,idempotency_key,phone_e164,expires_at)
                   VALUES (%s,%s,%s,%s,'created',%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING id,user_id,plan_id,amount_toman,duration_days,status,created_at,expires_at""",
                (user_id, plan["id"], plan["price_toman"], plan["duration_days"], idempotency_key,
                 user["phone_e164"], datetime.now(timezone.utc) + ORDER_TTL),
            )
            order = cur.fetchone()
            if order is None:
                cur.execute(
                    """SELECT id,user_id,plan_id,amount_toman,duration_days,status,created_at,expires_at
                       FROM payment_orders WHERE idempotency_key=%s""", (idempotency_key,)
                )
                order = cur.fetchone()
                if order["user_id"] != user_id or order["plan_id"] != plan["id"]:
                    raise ValueError("idempotency key was already used for a different order")
            order["plan_code"] = plan["code"]
            order["plan_title"] = plan["title"]
            # Price and duration are immutable order terms captured at creation.
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
    """Atomically deduplicate provider event, mark paid and activate one subscription."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.id,o.user_id,o.plan_id,o.amount_toman,o.status,o.expires_at,
                          o.duration_days,a.provider,a.provider_order_id,a.status AS attempt_status
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
            if row["attempt_status"] != "pending":
                raise PaymentRejected("payment attempt is not awaiting verification")
            if row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc):
                raise PaymentRejected("payment order has expired")
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
                    "SELECT order_id,processing_status FROM gateway_events WHERE provider=%s AND provider_event_id=%s",
                    (provider,verified.provider_event_id),
                )
                duplicate = cur.fetchone()
                if duplicate and duplicate["order_id"] == order_id and duplicate["processing_status"] == "processed":
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
            # Serialize subscription extension per user to avoid overlapping
            # renewal windows when two separate orders settle concurrently.
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
            cur.execute(
                "UPDATE gateway_events SET processing_status='processed',processed_at=now() WHERE id=%s",
                (event["id"],),
            )
            return {"already_processed": False, "order_id": order_id}


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


def expire_payment_orders(conn, *, limit: int = 200) -> int:
    """Cancel expired unpaid orders and release their checkout attempts."""
    if not 1 <= limit <= 2000:
        raise ValueError("limit must be between 1 and 2000")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """WITH due AS (
                     SELECT id FROM payment_orders
                     WHERE status IN ('created','pending') AND expires_at <= now()
                     ORDER BY expires_at
                     FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE payment_orders o
                   SET status='cancelled',updated_at=now()
                   FROM due WHERE o.id=due.id
                   RETURNING o.id""",
                (limit,),
            )
            order_ids = [row["id"] for row in cur.fetchall()]
            if order_ids:
                cur.execute(
                    """UPDATE payment_attempts SET status='expired'
                       WHERE order_id = ANY(%s) AND status IN ('initiated','pending')""",
                    (order_ids,),
                )
            return len(order_ids)


def request_full_refund(conn, *, order_id: UUID, requested_by: UUID,
                        idempotency_key: str, reason: str | None = None):
    """Create an auditable full-refund request; does not call a gateway."""
    if not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("a valid refund idempotency key is required")
    with conn.transaction():
        require_permission(conn, requested_by, "payments.read")
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,user_id,amount_toman,status FROM payment_orders
                   WHERE id=%s FOR UPDATE""", (order_id,)
            )
            order = cur.fetchone()
            if not order or order["status"] != "paid":
                raise ValueError("only paid orders can be refunded")
            cur.execute(
                """SELECT id FROM payment_attempts
                   WHERE order_id=%s AND status='verified'
                   ORDER BY verified_at DESC NULLS LAST LIMIT 1""", (order_id,)
            )
            attempt = cur.fetchone()
            if not attempt:
                raise ValueError("verified payment attempt not found")
            cur.execute(
                """SELECT id,order_id,payment_attempt_id,amount_toman,status,
                          idempotency_key,reason,created_at
                   FROM payment_refunds WHERE idempotency_key=%s""",
                (idempotency_key,),
            )
            prior = cur.fetchone()
            if prior:
                if prior["order_id"] != order_id or prior["amount_toman"] != order["amount_toman"]:
                    raise ValueError("refund idempotency key was used for another request")
                return prior
            cur.execute(
                """SELECT COALESCE(SUM(amount_toman),0) AS amount
                   FROM payment_refunds
                   WHERE order_id=%s AND status IN ('requested','pending','succeeded')""",
                (order_id,),
            )
            already_refunded = cur.fetchone()["amount"]
            if already_refunded:
                raise ValueError("a refund is already pending or has been issued")
            cur.execute(
                """INSERT INTO payment_refunds
                   (order_id,payment_attempt_id,amount_toman,status,idempotency_key,
                    reason,requested_by)
                   VALUES (%s,%s,%s,'requested',%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING id,order_id,payment_attempt_id,amount_toman,status,
                             idempotency_key,reason,created_at""",
                (order_id,attempt["id"],order["amount_toman"],idempotency_key,
                 (reason or "")[:300] or None,requested_by),
            )
            refund = cur.fetchone()
            if refund is None:
                cur.execute(
                    """SELECT id,order_id,payment_attempt_id,amount_toman,status,
                              idempotency_key,reason,created_at
                       FROM payment_refunds WHERE idempotency_key=%s""",
                    (idempotency_key,),
                )
                refund = cur.fetchone()
                if refund["order_id"] != order_id or refund["amount_toman"] != order["amount_toman"]:
                    raise ValueError("refund idempotency key was used for another request")
            cur.execute(
                """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                   VALUES (%s,'payment.refund.requested','payment_order',%s,
                           jsonb_build_object('refund_id',%s::text,'amount_toman',%s))""",
                (requested_by,str(order_id),str(refund["id"]),refund["amount_toman"]),
            )
            return refund


def claim_refund(conn, *, refund_id: UUID):
    """Atomically claim a refund for a worker; repeated claims cannot race."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE payment_refunds SET status='pending',updated_at=now()
                   WHERE id=%s AND status='requested'
                   RETURNING id,order_id,payment_attempt_id,amount_toman,
                             idempotency_key,provider_refund_id""",
                (refund_id,),
            )
            return cur.fetchone()


def finish_refund(conn, *, refund_id: UUID, provider_refund_id: str):
    if not provider_refund_id or len(provider_refund_id) > 160:
        raise ValueError("invalid provider refund reference")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT order_id,amount_toman,status FROM payment_refunds
                   WHERE id=%s FOR UPDATE""", (refund_id,)
            )
            refund = cur.fetchone()
            if not refund or refund["status"] != "pending":
                raise ValueError("refund is not pending")
            cur.execute(
                """UPDATE payment_refunds SET status='succeeded',provider_refund_id=%s,
                   completed_at=now(),updated_at=now() WHERE id=%s""",
                (provider_refund_id,refund_id),
            )
            cur.execute(
                """UPDATE payment_orders SET status='refunded',updated_at=now()
                   WHERE id=%s AND status='paid'""", (refund["order_id"],)
            )
            cur.execute(
                """UPDATE subscriptions SET status='refunded'
                   WHERE payment_order_id=%s AND status='active'""",
                (refund["order_id"],),
            )
            cur.execute(
                """INSERT INTO admin_audit_logs(action,target_type,target_id,after_state)
                   VALUES ('payment.refund.succeeded','payment_order',%s,
                           jsonb_build_object('refund_id',%s::text,'provider_refund_id',%s::text))""",
                (str(refund["order_id"]),str(refund_id),provider_refund_id),
            )


def release_refund_for_retry(conn, *, refund_id: UUID, safe_to_retry: bool):
    """Only a definitive provider rejection is retryable; ambiguous results stay pending."""
    with conn.transaction():
        with conn.cursor() as cur:
            status = "requested" if safe_to_retry else "pending"
            cur.execute(
                """UPDATE payment_refunds SET status=%s,updated_at=now()
                   WHERE id=%s AND status='pending'""", (status,refund_id)
            )


def enqueue_stale_payment_reconciliation(conn, *, older_than_minutes: int = 30,
                                         limit: int = 200) -> int:
    if not 1 <= older_than_minutes <= 10080 or not 1 <= limit <= 2000:
        raise ValueError("invalid reconciliation window or limit")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO payment_reconciliation_cases
                   (order_id,payment_attempt_id,reason,status)
                   SELECT o.id,a.id,'stale_pending_payment','open'
                   FROM payment_orders o
                   JOIN payment_attempts a ON a.order_id=o.id
                   WHERE o.status='pending' AND a.status='pending'
                     AND a.created_at < now() - (%s * interval '1 minute')
                   ORDER BY a.created_at
                   LIMIT %s
                   ON CONFLICT (payment_attempt_id,reason) DO NOTHING""",
                (older_than_minutes,limit),
            )
            return cur.rowcount


def list_payment_reconciliation_cases(conn, *, limit: int = 100):
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.id,c.order_id,c.payment_attempt_id,c.reason,c.status,c.created_at,
                      o.amount_toman,o.status AS order_status,a.provider,a.provider_order_id
               FROM payment_reconciliation_cases c
               JOIN payment_orders o ON o.id=c.order_id
               JOIN payment_attempts a ON a.id=c.payment_attempt_id
               WHERE c.status='open' ORDER BY c.created_at LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()



def list_requested_refunds(conn, *, limit: int = 100):
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM payment_refunds WHERE status='requested'
               ORDER BY created_at LIMIT %s""", (limit,)
        )
        return [row["id"] for row in cur.fetchall()]


def resolve_payment_reconciliation_case(conn, *, case_id: UUID, resolved_by: UUID,
                                        resolution_note: str, dismissed: bool = False):
    note = (resolution_note or "").strip()
    if not note or len(note) > 500:
        raise ValueError("a resolution note between 1 and 500 characters is required")
    status = "dismissed" if dismissed else "resolved"
    with conn.transaction():
        require_permission(conn, resolved_by, "payments.read")
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE payment_reconciliation_cases
                   SET status=%s,resolution_note=%s,resolved_by=%s,resolved_at=now()
                   WHERE id=%s AND status='open'
                   RETURNING id,order_id,payment_attempt_id,status,resolution_note,resolved_at""",
                (status,note,resolved_by,case_id),
            )
            case = cur.fetchone()
            if not case:
                raise LookupError("open reconciliation case not found")
            cur.execute(
                """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                   VALUES (%s,%s,'payment_reconciliation_case',%s,
                           jsonb_build_object('status',%s::text,'note',%s::text))""",
                (resolved_by,f"payment.reconciliation.{status}",str(case_id),status,note),
            )
            return case
