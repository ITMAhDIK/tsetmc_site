"""Bounded SaaS payment maintenance tasks; schedule externally (e.g. cron)."""
import os
import psycopg
from psycopg.rows import dict_row

from .domain import PaymentNotConfigured
from .gateways import get_gateway
from .repository import (
    claim_refund, enqueue_stale_payment_reconciliation, expire_payment_orders,
    finish_refund, release_refund_for_retry, list_requested_refunds,
)


def run_payment_maintenance(conn):
    expired = expire_payment_orders(conn, limit=500)
    reconciliation_cases = enqueue_stale_payment_reconciliation(
        conn, older_than_minutes=30, limit=500
    )
    refund_results = [
        process_refund(conn, refund_id=refund_id)
        for refund_id in list_requested_refunds(conn, limit=100)
    ]
    return {
        "expired_orders": expired,
        "reconciliation_cases_created": reconciliation_cases,
        "refunds_processed": sum(1 for item in refund_results if item["processed"]),
        "refunds_waiting_for_provider": sum(1 for item in refund_results if not item["processed"]),
    }


def process_refund(conn, *, refund_id):
    """Execute a claimed refund with the provider's stable idempotency key.

    Unknown network outcomes remain pending for manual/provider reconciliation.
    """
    refund = claim_refund(conn, refund_id=refund_id)
    if not refund:
        return {"processed": False, "reason": "not_claimable"}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT provider,provider_order_id FROM payment_attempts
               WHERE id=%s""", (refund["payment_attempt_id"],)
        )
        attempt = cur.fetchone()
    if not attempt or not attempt["provider_order_id"]:
        release_refund_for_retry(conn, refund_id=refund_id, safe_to_retry=False)
        return {"processed": False, "reason": "missing_provider_reference"}
    try:
        gateway = get_gateway(attempt["provider"])
        provider_refund_id = gateway.refund(
            provider_order_id=attempt["provider_order_id"],
            amount_toman=refund["amount_toman"],
            idempotency_key=refund["idempotency_key"],
        )
        finish_refund(conn, refund_id=refund_id, provider_refund_id=provider_refund_id)
        return {"processed": True, "refund_id": str(refund_id)}
    except PaymentNotConfigured:
        release_refund_for_retry(conn, refund_id=refund_id, safe_to_retry=True)
        return {"processed": False, "reason": "provider_not_configured"}
    except Exception:
        # A timeout may occur after the provider has executed the refund. Keep
        # the request pending; never issue a second non-idempotent refund.
        release_refund_for_retry(conn, refund_id=refund_id, safe_to_retry=False)
        return {"processed": False, "reason": "provider_outcome_requires_reconciliation"}


def main():
    dsn = os.getenv("SAAS_DATABASE_URL", "")
    if not dsn:
        raise SystemExit("SAAS_DATABASE_URL is required")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        print(run_payment_maintenance(conn))


if __name__ == "__main__":
    main()
