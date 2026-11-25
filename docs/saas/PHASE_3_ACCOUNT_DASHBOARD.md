# Phase 3 — Account and dashboard API

## Scope
This phase adds the authenticated account-facing API foundation:
- Read and update the user's display name.
- Read and update allow-listed UI preferences (theme, calendar, currency, locale).
- List active sessions, revoke a selected other session, or revoke all other sessions.
- Read subscription history.
- Read server-resolved feature entitlements for premium analytics, ownership graphs
  and investor history.

## API routes
- GET/PATCH /account/profile
- GET/PATCH /account/preferences
- GET /account/sessions
- DELETE /account/sessions/{session_id}
- POST /account/sessions/revoke-others
- GET /account/subscriptions
- GET /account/features

All routes require a valid authenticated session. A user can only read or mutate
their own account records. The current session is identified by its database
session UUID and cannot be revoked through the "other sessions" endpoint; use
/auth/logout to end it.

## Data and security boundaries
- All persistence uses SAAS_DATABASE_URL and the SaaS PostgreSQL schema.
- No market-data database access, joins or schema changes are introduced.
- Preference keys and values are allow-listed; SQL column names are never accepted
  directly from client input.
- Feature access is computed server-side. Hiding UI elements is not authorization;
  protected feature APIs must call the same entitlement policy.
- Session tokens are never returned by account endpoints. User-agent metadata is
  shown only for the user's own sessions.

## Not included
This is an API foundation, not a finished dashboard frontend. Payment history,
invoice downloads, email/Google identity management, password reset, and
production deployment remain separate work. Subscription history reflects
records already written by a future payment lifecycle; this phase does not
create or activate subscriptions.

## Validation status
Unit tests cover preference allow-lists and protection against revoking the
current session. PostgreSQL integration tests and runtime API tests must still
be run against a disposable SaaS database before production use.
