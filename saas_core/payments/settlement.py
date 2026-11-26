"""Serialize provider-event handling and make callback retries idempotent."""
from .domain import PaymentRejected
from .repository import settle_verified_payment


def settle_provider_callback(conn, *, order_id, attempt_id, provider, verified, payload_sha256):
    """Return success for an already-processed retry of the same order/event.

    The advisory transaction lock closes the race between two simultaneous
    callbacks carrying the same provider event ID. The repository's settlement
    runs in a nested transaction/savepoint while the lock remains held.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (provider, verified.provider_event_id),
            )
            cur.execute(
                """SELECT order_id, processing_status FROM gateway_events
                   WHERE provider=%s AND provider_event_id=%s""",
                (provider, verified.provider_event_id),
            )
            existing = cur.fetchone()
            if existing:
                if existing["order_id"] != order_id:
                    raise PaymentRejected("provider event is already linked to another order")
                if existing["processing_status"] == "processed":
                    return {"already_processed": True, "order_id": order_id}
                raise PaymentRejected("provider event is already being processed")
        return settle_verified_payment(
            conn,
            order_id=order_id,
            attempt_id=attempt_id,
            provider=provider,
            verified=verified,
            payload_sha256=payload_sha256,
        )
