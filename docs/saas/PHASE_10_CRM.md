# Phase 10 — Internal CRM foundation

## Goal and boundary

Phase 10 adds a staff-only customer relationship layer to the SaaS platform.
It uses the stable SaaS `users.id` UUID as the customer identity and stays
inside the dedicated SaaS PostgreSQL database.

CRM is not a second customer directory. It does not copy phone numbers,
email addresses, payment credentials, bank details, OTPs, raw analytics events
or support-ticket message bodies. It does not connect to the Market SQLite or
Sidecar and it does not grant data-management access.

The CRM service is an application/domain service exposed by the existing SaaS
FastAPI deployment; it is not a separately deployed microservice.

## Delivered functionality

- Customer directory with bounded search, status/stage/assignee filters and
  pagination. Search is limited to username, public ID and display name.
- Customer lifecycle stage: `new`, `active`, `at_risk` or `churned`.
- Assignment of a customer profile to an active CRM staff member.
- Append-only interaction timeline with channel, short subject/summary and
  timezone-aware event time.
- Follow-up tasks with customer, assignee, due time and `open`,
  `completed` or `cancelled` lifecycle.
- Immutable administrative audit entries for CRM reads and writes.
- Production readiness now requires the three CRM schema tables to exist.

## Authorization and privacy

Migration `014_customer_relationship_management.sql` introduces
`crm.read` and `crm.manage` permissions. They are granted to the `admin`
and `superadmin` roles only; the `support` role does not receive CRM access.
CRM API routes also require a TOTP-enrolled account and a recent MFA
verification for the current session.

CRM-authorized staff may enroll their own MFA device. This does not grant
`superadmin.console.access` or access to the separate Superadmin console.
The existing Superadmin routes retain their own stricter permission check.

Customer list/detail responses return verification booleans, not raw phone or
email values. Search does not match contact addresses. CRM activity summaries
must not include passwords, OTPs, payment secrets, bank details, full payment
payloads or private ticket contents. Interaction records are immutable; a
correction is recorded as a new interaction. Follow-up state changes and
customer-profile changes are audited.

## API surface

All routes are under `/crm` and require staff authentication, CRM permission
and session-bound MFA:

- `GET /crm/customers`
- `GET /crm/customers/{customer_id}`
- `PATCH /crm/customers/{customer_id}/profile`
- `GET /crm/customers/{customer_id}/interactions`
- `POST /crm/customers/{customer_id}/interactions`
- `GET /crm/followups`
- `POST /crm/followups`
- `PATCH /crm/followups/{followup_id}/status`

The API validates bounded pagination, search length, lifecycle stages, event
channels, task statuses and timezone-aware timestamps. Assignees must be active
users with `crm.manage`.

## Database

Migration 014 adds:

- `crm_customer_profiles`: one optional stage/assignee record per SaaS user.
- `crm_interactions`: immutable staff interaction history.
- `crm_followups`: due-dated, assignable tasks with a constrained status.

All foreign keys target SaaS users only. No market-data table, cross-database
foreign key or copied contact directory is introduced.

## Test coverage

- `tests/saas/test_crm_phase10.py`: API registration, input validation,
  pagination bounds, migration constraints and schema-readiness coverage.
- `tests/saas/test_crm_phase10_postgres.py`: PostgreSQL API integration for
  CRM staff MFA, customer privacy, stage assignment, interaction immutability,
  follow-up completion, audit records, missing records and exact role grants.
- `tests/test_portfolio_service.py::test_phase_10_crm_runs_alongside_market_without_cross_database_access`:
  cross-platform test that runs the Market HTTP API alongside CRM on the
  separately named disposable SaaS PostgreSQL database and verifies that CRM
  operations do not alter Market source/Sidecar behavior.
- The Python CI workflow runs the focused Phase 10 tests before the complete
  Python/PostgreSQL suite.

Run Phase 10 tests locally after applying all migrations to the named
disposable SaaS test database:

```bash
python -m pytest -q \
  tests/saas/test_crm_phase10.py \
  tests/saas/test_crm_phase10_postgres.py \
  tests/test_portfolio_service.py::test_phase_10_crm_runs_alongside_market_without_cross_database_access
```

## Explicitly deferred

This phase delivers the CRM schema and backend API, not a finished staff UI.
The CRM browser workspace, automatic task reminders, provider-backed email/SMS
campaigns, marketing consent/preferences, lead capture for non-registered
prospects, CRM exports, advanced segmentation and workflow automation remain
separate work. No outbound message is sent by this phase. Any future outreach
must use approved channel consent, provider credentials, rate limits and
auditable delivery records.

CRM API readiness and green CI are not proof of a deployed CRM service or a
legal basis for marketing contact. Production activation still requires the
normal Phase 9 host, secrets, privacy/legal and independent security gates.
