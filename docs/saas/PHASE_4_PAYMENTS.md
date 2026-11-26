# Phase 4 — Subscriptions and payments

## Implemented lifecycle
- Server-priced subscription orders; clients cannot set the amount.
- Verified-phone purchase gate and idempotent order creation.
- HTTPS hosted-checkout contract and one active checkout per order.
- Provider-neutral verification contract; amount, provider order reference and event identity are checked server-side.
- Atomic order settlement, one subscription per paid order, and serialized renewal extension per user.
- Provider-event callback retries are serialized with a PostgreSQL advisory transaction lock.
- Expired orders are cancelled in bounded batches and their active checkout attempts are expired.
- Full-refund requests are idempotent, permission-gated, audited and queued for a worker.
- Successful refund completion updates the order and linked subscription in one transaction.
- Stale pending payments are placed in an admin reconciliation queue with a review API.
- Migration 002 is safe to re-run and creates refund and reconciliation structures.

## API
- POST /payments/orders
- POST /payments/orders/{order_id}/checkout
- GET /payments/orders/{order_id}
- POST /payments/providers/{provider}/return
- POST /payments/admin/orders/{order_id}/refunds
- POST /payments/admin/reconciliation/run
- GET /payments/admin/reconciliation

## Operations
Run the bounded maintenance functions from a scheduled worker:
- `expire_payment_orders(conn)`
- `enqueue_stale_payment_reconciliation(conn)`

Refund execution is intentionally a separate worker action. It must claim a request, call the provider using its stable idempotency key, and only then record the provider refund reference with `finish_refund`. An ambiguous timeout must remain pending for reconciliation, never be blindly retried as a new refund.

## Gateway limitation — not production complete
SnapPay and DigiPay adapters still fail closed. Live initiation, server-to-server verification, callback signature/authenticity, refunds, and sandbox E2E cannot responsibly be implemented without each merchant's current official integration guide, merchant account, and sandbox credentials. Do not accept live payments or expose callbacks publicly until those are supplied and verified. No gateway URL, signature scheme, credential or callback field is guessed.

## Validation
CI now provisions a disposable PostgreSQL 16 service, applies the isolated SaaS schema/migration, and runs real database integration tests alongside unit tests. No production database or real money is touched.
