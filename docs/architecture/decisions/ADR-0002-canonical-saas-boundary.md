# ADR-0002: One canonical SaaS identity and commercial data authority

- **Status:** Accepted for target architecture; integration deferred
- **Date:** 2026-11-26
- **Context:** The repository has multiple unmerged SaaS implementation lines. One uses `site_accounts.py` and a separate SQLite database. Another uses FastAPI modules under `saas_core/` with PostgreSQL migrations. Both model overlapping identity/account functionality. Mixing them would create competing session, role, subscription and payment authorities.
- **Decision:** The target architecture will use the PostgreSQL-backed `saas_core` line as the canonical SaaS boundary. Do not merge the legacy `site_accounts.py` account authority into the target. Integrate the selected FastAPI implementation only through its ordered, reviewed branch/PR chain after the baseline market API is stabilized.
- **Boundary:** SaaS owns users, verified identities, sessions, roles, plans, subscriptions, orders, payment attempts, wallet ledger, referrals, support, consent and administrative audit. It does not own market or shareholder source records.
- **Required before integration:**
  1. Reconcile all draft PRs and select a single coherent commit chain.
  2. Confirm endpoint prefix, cookie domain, CORS, CSRF and gateway routing contract.
  3. Migrate any real existing account records with explicit source/target counts and conflict handling.
  4. Verify that the deployed API uses only its dedicated PostgreSQL database.
  5. Keep payments/SMS/Google OAuth fail-closed until provider credentials and sandbox evidence exist.
- **Consequences:** The baseline `main` branch remains unchanged in identity/commercial behavior during Phase 0 and Phase 1. This ADR records the target; it does not merge or activate SaaS code.
