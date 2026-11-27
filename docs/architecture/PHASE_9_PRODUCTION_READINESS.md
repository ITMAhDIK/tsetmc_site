# Phase 9 — Production readiness and cross-phase verification

## Goal

Phase 9 verifies that the Phase 0–8 system can be operated as two deliberately
separated deployment units, that the security and recovery controls fail closed,
and that the critical runtime paths continue to work when the Market and SaaS
platforms are exercised together.

Phase 9 is a release-readiness and verification phase. It does not split the
Market domain services into additional network microservices and it does not
claim that a production host has been provisioned.

## Runtime boundaries under test

- **Market platform (Phases 0–6):** the HTTP API composes the Market,
  Ownership, Portfolio and Screener services. Its source holdings SQLite is
  opened read-only; its derived Sidecar is separate and rebuildable. Public
  production mode permits only market reads and side-effect-free Screener
  query/export operations. User-state writes and legacy Market admin routes
  are not public.
- **SaaS platform (Phases 7–8):** the FastAPI application uses a dedicated
  PostgreSQL database. Authentication, analytics, wallet, payments, support
  and staff/admin routes do not join to the Market SQLite or Sidecar.
- **Browser edge:** the SaaS Edge serves only the approved public pages and
  proxies the same-origin API prefix. Internal metrics and operational routes
  are not exposed through the public Caddy site.

The two platforms may be deployed independently. They are not one shared
database and are not represented as six independently deployed microservices.

## Delivered implementation

### Production configuration and service checks

- Production startup validates explicit HTTPS hosts/origins, a verified-TLS
  remote PostgreSQL DSN, the expected database name, separate secrets,
  privileged bootstrap phone configuration, bounded connection-pool settings
  and the private metrics token.
- The SaaS API has liveness and database/schema readiness endpoints. A missing
  production connection pool or an unexpected database fails closed.
- The Market API has separate health and readiness endpoints. Public mode has
  an explicit CORS allow-list and denies Sidecar writes and legacy admin routes.
- Production Compose files, non-root images, Caddy routing, restricted Edge
  page packaging, and container smoke checks are included.

### Security and audit controls

- Session tokens are random and only their digests are stored. Passwords use
  Argon2id; OTP challenges are HMAC-protected and domain-separated.
- TOTP MFA is required for privileged admin routes and is bound to the active
  session. OTP replay, expired MFA verification and unconfigured production
  secrets fail closed.
- Legal acceptance is tied to the OTP challenge/version and stored
  append-only. Signup is blocked until the owner-approved legal versions are
  configured.
- Staff access, support notes, payment review, wallet changes and analytics
  inspection are permission-gated and audited. Payment reconciliation notes do
  not themselves settle a payment or activate a subscription.
- Prometheus metrics use a separate bearer token and low-cardinality labels.
  The metrics path is private and is not proxied by the public Edge.

### Backup, restore and operations

- PostgreSQL backups use custom-format dumps, archive verification,
  age-encryption, SHA-256 sidecars and atomic publication.
- Restore verification requires an explicitly named non-production target,
  checks the archive and checksum, and restores into a separate scratch
  database. Production-database name collisions are refused.
- The off-host uploader accepts encrypted archives only, uses immutable copy
  semantics, reads objects back and verifies their bytes/checksum.
- Private Prometheus configuration, alert rules, backup/restore runbooks,
  incident guidance and daily systemd timer templates are included.
- The bounded staging probe is GET-only and limited to `/health` and
  `/ready`. It requires a literal non-production approval, an exact hostname
  confirmation, HTTPS, public DNS and bounded duration/rate/concurrency. It
  does not follow redirects or retain response bodies.

## Phase 9 test plan

### Phase-specific tests

- `tests/saas/test_phase9_security_baseline.py`: password/session/OTP
  primitives, secret separation, production configuration, TLS, pool limits,
  metrics authorization and migration-history safety.
- `tests/saas/test_phase9_load_probe.py`: explicit staging approval, HTTPS and
  origin validation, private/reserved target rejection, exact host confirmation,
  request-rate bounds, GET-only path selection, redirect denial, response-body
  privacy, failure accounting and overdue-request dropping.
- `tests/ops/test_offsite_backup.py`: immutable upload, encrypted-only input,
  checksum verification, remote read-back and corruption detection.
- `tests/saas/test_postgres_integration_phase9.py`: real PostgreSQL-backed
  authentication/session revocation, role boundaries, consent-aware analytics,
  payment idempotency, wallet race protection, admin audit immutability,
  connection-pool identity, access-log privacy, MFA-protected staff access,
  signup bootstrap and route-level authorization.

### Cross-phase integration

`tests/test_portfolio_service.py::test_phase_9_cross_platform_end_to_end_smoke`
runs the two application boundaries in one test:

1. Starts the Market HTTP handler against disposable SQLite source and Sidecar
   files and verifies public market reads.
2. Confirms public Market writes and legacy admin routes remain denied.
3. Connects the SaaS FastAPI dependency to the explicitly named disposable
   PostgreSQL database and verifies health and authenticated identity.
4. Confirms the superadmin route is blocked before MFA, then enrolls and
   confirms TOTP for that session and verifies the admin dashboard is available.
5. Re-checks Market reads after SaaS admin activity and verifies the source
   SQLite remains query-only and the Sidecar remains separate.

This test uses no production database, live SMS delivery, payment provider or
external TSETMC request. It proves the application-level integration boundary,
not provider delivery or production infrastructure.

## CI gates

The Python test workflow:

1. Starts an ephemeral PostgreSQL 16 service.
2. Applies and checksum-validates all SaaS migrations 001–013.
3. Runs the focused Phase 9 security, staging-probe and cross-platform tests.
4. Runs the complete Python and PostgreSQL integration suite.

The security workflow separately runs static/security analysis, Python
compilation, container builds and smoke checks, production Compose/Caddy
validation, dependency auditing, and an encrypted PostgreSQL backup plus
isolated restore drill.

Run the focused Phase 9 checks locally with:

```bash
python -m pytest -q \
  tests/saas/test_phase9_load_probe.py \
  tests/saas/test_phase9_security_baseline.py \
  tests/test_portfolio_service.py::test_phase_9_cross_platform_end_to_end_smoke
```

Run the full suite with:

```bash
python -m pytest -q
```

The cross-platform test requires `SAAS_TEST_DATABASE_URL` and
`SAAS_TEST_DATABASE_NAME` to point to a disposable database whose schema has
already been migrated. It intentionally skips when that test database is not
configured; CI supplies it explicitly.

## Remaining external release gates

A passing CI run is not a production deployment. Before public go-live, the
operator must still provision and verify:

- DNS, TLS certificates, firewall rules, host networking and private registry
  image digests.
- A dedicated managed PostgreSQL database, separately provisioned migration,
  runtime and backup roles, verified TLS, protected secrets and a tested
  recovery procedure.
- Real SMS provider credentials, an approved sender and carrier-delivery
  verification. The test suite uses a fake sender.
- Official payment-provider sandbox credentials and verified callback/replay
  testing. Provider adapters remain fail-closed without their approved
  configuration.
- Owner/legal-approved Terms and Privacy text and matching immutable version
  values. Placeholder legal pages intentionally block signup.
- A real off-host storage account, credential custody, provider-side
  retention/object-lock and a restore-access drill. CI uses a fake remote for
  uploader tests.
- Alertmanager receiver, on-call ownership/escalation, notification test,
  host/database capacity alerts and an incident exercise.
- An approved staging deployment before running the bounded load probe.
- Independent penetration testing and security/legal approval.

Do not run the staging load probe against production. Do not point test,
migration, backup or restore scripts at Market, collector, TSETMC or production
databases. No production service or production database is created or modified
by Phase 9 CI.
