# Tloomberg SaaS Core

Independent foundation for accounts, roles, subscriptions, payments, wallet,
referrals, support and product analytics.

## Data separation
- Existing market data remains in its existing database and read-only service.
- SaaS identity and commercial records use a dedicated PostgreSQL database.
- No cross-database foreign keys or shared tables connect these domains.
- Analytics is allow-listed and must never collect secrets, raw form values,
  OTPs, passwords, payment data or private ticket content.

## Current implementation
- \`schema/001_initial.sql\`: PostgreSQL schema and initial plans.
- \`domain/wallet.py\`: pure cash/gift wallet rules.
- \`../tests/saas/test_wallet_rules.py\`: wallet policy tests.

## Wallet rules
All money is integer Toman. Cash is user-funded and potentially withdrawable;
gift is promotional/referral credit and non-withdrawable. Gift is consumed
before cash for eligible service purchases. Balances are derived from an
append-only ledger; corrections require compensating entries. Production
withdrawal logic must subtract pending/approved reservations under transaction
locks. The pure domain module is not a payment processor.

## Apply schema
Create a dedicated SaaS PostgreSQL database, then apply every numbered migration in order. In PowerShell:
\`\`\`powershell
Get-ChildItem saas_core/schema/*.sql | Sort-Object Name | ForEach-Object {
    psql "$env:SAAS_DATABASE_URL" -v ON_ERROR_STOP=1 -f $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "Migration failed: $($_.Name)" }
}
\`\`\`
Never point this URL at the market-data database. Migrations are designed to be re-runnable; back up the SaaS database before applying production migrations.

Payment gateways, SMS, Google OAuth, production authentication and withdrawal
processing are intentionally not represented as live integrations yet.
They require merchant credentials, current provider contracts, secrets,
sandbox verification and dedicated service implementations.


## Support (Phase 6)
- `support/service.py` owns ticket and FAQ persistence.
- `support/api.py` exposes public published FAQs, authenticated owner-scoped tickets,
  staff queue/assignment/status/replies and private internal notes.
- Staff capabilities are permission-gated by `tickets.manage`; FAQ publishing is
  separately gated by `faq.manage`.
- User ticket reads filter internal notes at the SQL query; no client-side hiding.
- Migration `schema/004_support.sql` adds query indexes only and is re-runnable.
- Support records remain in the isolated SaaS PostgreSQL database.


## Product analytics (Phase 7)
- analytics/service.py implements opt-in consent, allow-listed event ingestion and aggregate reports.
- analytics/api.py exposes authenticated ingestion and permission-gated aggregate/user-level analytics.
- Migration schema/005_analytics.sql adds event idempotency and analytics permissions; it is re-runnable.
- Product analytics is off until the account explicitly opts in; consent changes are kept in an append-only history and each event records the consent version. User-level analysis requires analytics.users.read and is audited.
- Raw form values, secrets, payment data and support-ticket content are not accepted as event payloads.
