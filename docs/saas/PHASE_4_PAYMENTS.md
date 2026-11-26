# Phase 4 — Subscriptions and payments

## Implemented foundation
- Dedicated SaaS PostgreSQL migration `002_payment_lifecycle.sql` adds order expiry/currency metadata,
  payment-attempt amount/currency/failure metadata, subscription-to-order linkage,
  and an idempotent refund-request table.
- Order creation reads the active plan price from the database; clients cannot
  submit or override the amount.
- Purchase requires a verified phone number.
- Order creation accepts an idempotency key and rejects reuse across a different
  user or plan.
- Checkout and order-status API routes are mounted on the SaaS API.
- Provider-neutral gateway protocol defines initiate, verify and refund.
- Payment settlement validates the server-verified amount, deduplicates gateway
  events, marks the order paid and activates exactly one subscription atomically.
- Renewals extend from the latest active subscription end date rather than
  truncating remaining paid time.
- Browser redirects are not trusted as proof of payment.

## API routes
- POST /payments/orders — create order from a server-side plan price.
- POST /payments/orders/{order_id}/checkout — initiate hosted checkout.
- GET /payments/orders/{order_id} — read own order status.
- POST /payments/providers/{provider}/return — server-side provider verification
  entry point (must be wired to the provider's official verification contract).

## Provider onboarding is intentionally blocked
SnapPay and DigiPay gateway classes currently fail closed with
`PaymentNotConfigured`. No API URLs, credential names, signature algorithms,
callback fields or refund payloads have been guessed. Live adapters require the
merchant's current official integration pack, merchant onboarding and sandbox
credentials. Do not expose payment endpoints publicly until those adapters,
callback authenticity checks, rate limits, CSRF/CORS, and sandbox tests exist.

## Refunds and reconciliation
The migration stores refund requests and provider references, but refund
execution, partial-refund policy, settlement reports, scheduled reconciliation,
and operational admin review are not implemented yet. Payment history remains
immutable; corrections must be represented as compensating financial records.

## Validation status
Focused unit tests cover amount validation, verified-amount matching, failed
verification rejection, and the fail-closed gateway behavior. PostgreSQL
integration tests, concurrent idempotency tests, provider sandbox tests and
end-to-end callback tests have not been run.
