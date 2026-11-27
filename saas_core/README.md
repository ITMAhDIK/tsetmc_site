# Tloomberg SaaS Core

Independent SaaS application for account preferences, support, staff operations,
CRM and product analytics. Identity and Commerce are separately deployed services.

## Data separation
- Existing market data remains in its existing database and read-only service.
- Identity accounts/credentials use the dedicated Identity PostgreSQL database.
- Subscription, payment and wallet records use the dedicated Commerce PostgreSQL database.
- SaaS keeps non-secret user projections and owns support, CRM and analytics.
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
Create a dedicated SaaS PostgreSQL database, then run:
\`\`\`powershell
psql "$env:SAAS_DATABASE_URL" -v ON_ERROR_STOP=1 -f saas_core/schema/001_initial.sql
\`\`\`
Never point this URL at the market-data database.

Payment gateways, SMS, Google OAuth, production authentication and withdrawal
processing are intentionally not represented as live integrations yet.
They require merchant credentials, current provider contracts, secrets,
sandbox verification and dedicated service implementations.


## Microservices boundary

The SaaS API delegates browser authentication to Identity and commercial operations
to Commerce. Payment and wallet routers are no longer mounted by the SaaS app.
The Commerce implementation, own database schema, independent migration runner,
container, Edge routing and dry-run-first legacy data migration are documented in
`docs/commerce/PHASE_2_COMMERCE_SERVICE.md`.

Run Commerce migrations with:
```powershell
python scripts/manage_commerce_migrations.py --apply
```
Use the dedicated `COMMERCE_MIGRATION_DATABASE_URL` and verify the expected database
name before applying migrations. Never point it at the Market or Identity database.
