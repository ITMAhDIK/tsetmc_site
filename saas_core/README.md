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
Create a dedicated SaaS PostgreSQL database, then run:
\`\`\`powershell
psql "$env:SAAS_DATABASE_URL" -v ON_ERROR_STOP=1 -f saas_core/schema/001_initial.sql
\`\`\`
Never point this URL at the market-data database.

Payment gateways, SMS, Google OAuth, production authentication and withdrawal
processing are intentionally not represented as live integrations yet.
They require merchant credentials, current provider contracts, secrets,
sandbox verification and dedicated service implementations.
