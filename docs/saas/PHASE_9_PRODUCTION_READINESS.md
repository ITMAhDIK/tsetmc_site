# Phase 9 — Production readiness

## Release posture

**Status: NOT READY FOR PUBLIC PRODUCTION.** Phase 9 now has a substantial code and CI foundation, including real PostgreSQL integration tests and an ephemeral backup/restore rehearsal. That does not certify the actual operating environment. The prerequisite phase PRs remain Draft/unmerged, and no production host, managed database, off-host backup target, monitoring destination, provider credentials, legal entity or applicable jurisdictions have been supplied and verified.

Phase 9 is a release-readiness programme for the full Phases 1–8 dependency chain. It is not a feature milestone that can be declared complete solely because the repository CI is green.

## System boundary and assets

| Component | Trust boundary / responsibility | Data or privilege to protect |
|---|---|---|
| Browser and GitHub Pages | Untrusted client; static presentation only | Session cookie, user input, analytics consent |
| SaaS FastAPI service | Public-facing application boundary; hosted separately from Pages | Authentication, authorization, payment state, wallet mutations, support and admin actions |
| Dedicated SaaS PostgreSQL | SaaS persistence boundary | Identity, sessions, phone data, payments, wallet ledgers, support messages, analytics and audit records |
| Payment/SMS/bank providers | External systems; trust only authenticated provider responses | Payment references, OTP delivery and bank-vault references |
| GitHub repository and Actions | Source/build supply-chain boundary | Source integrity, workflow permissions and deployment credentials |
| TSETMC market-data collector/database/API | Separate product boundary | Must not be connected to or modified by SaaS operational tooling |

Sensitive assets include password hashes, session-token digests, OTP challenge hashes, phone numbers, encrypted notification payloads, provider references, wallet ledger entries, support messages/internal notes, analytics identifiers, bank-vault tokens and administrator audit records.

## Threat model and controls

| Threat / abuse case | Implemented engineering controls | Remaining evidence required |
|---|---|---|
| Password guessing and user enumeration | Argon2id password hashing; unknown/inactive account login performs dummy Argon2 verification | Distributed abuse controls, recovery-flow review and production login monitoring |
| OTP guessing and parallel request races | OTP HMAC; one-time expiring challenges; five-attempt ceiling; per-phone 3-per-15-minute throttle serialized by a transaction advisory lock; encrypted outbox payloads | Edge/IP/device rate limiting, SMS-provider abuse monitoring and real SMS sandbox/production tests |
| Session theft / browser request forgery | Opaque random sessions; only token digests stored; HttpOnly/SameSite cookies; Secure production cookie requirement; cookie-write Origin/Referer check; explicit HTTPS CORS allow-list | TLS/proxy deployment verification, cookie-domain review, secret rotation and external security review |
| Privilege escalation / broken object-level authorization | Server-side RBAC; superadmin console requires console plus action capability; tested standard/support/superadmin boundary and cross-user ticket isolation | Complete route-by-route permission matrix, full independent review and additional tenant/object isolation tests |
| Payment callback forgery/replay/amount mismatch | Server-side verification contract; amount/order/provider checks; exact provider-event replay is idempotent; reused event ID with different payload is rejected; unconfigured adapters fail closed | Current official provider packs, signature validation, merchant sandbox tests and concurrent callback/retry testing with the actual provider |
| Wallet double-spend or unauthorized adjustment | Append-only ledger trigger; row-locked cash/gift ledger; negative-balance prevention; exact idempotent adjustment replay; concurrent debit integration test | Broader concurrency/load tests, ledger reconciliation and payment/withdrawal provider workflows |
| Tampering with administrator history | PostgreSQL trigger makes administrator audit logs append-only | Independent audit review, monitoring alerts and access recertification |
| Database drift and concurrent migration runners | Checksummed migration ledger; advisory lock; transaction-per-migration; changed/unknown migrations rejected; non-empty untracked schemas rejected | Existing database baseline adoption procedure and real managed-DB permissions test |
| Database connection exhaustion | Bounded per-worker psycopg pool with min/max/timeout controls | Production connection budget against actual worker count, host limits and real traffic patterns |
| Database loss/operator error | Custom-format backup, checksum, isolated single-transaction restore; successful PostgreSQL 16 CI rehearsal | Encrypted off-host retention, immutable backup policy, scheduled execution, actual production restore drill, RPO/RTO |
| Dependency/build compromise | pip-audit, high-severity/high-confidence Bandit scan, Dependabot, non-root Docker image build and read-only container configuration | Pin all external GitHub Actions by reviewed commit SHA; image scanning/signing, SBOM retention and remediation SLA |
| Insider or compromised administrator | Audited privileged changes, immutable audit records, out-of-band superadmin bootstrap/removal | Dual approval for money operations, access recertification and operational alert routing |

This threat model is a working engineering document, not an independent threat assessment or penetration test. Revisit it when real hosting, providers, operators and user jurisdictions are selected.

## Implemented in Phase 9

### Application security and database access
- Production startup fails closed when the database identity, independent OTP/encryption secrets, secure cookie, explicit host allow-list or HTTPS browser origins are missing.
- Explicit CORS allow-list, TrustedHost middleware, cookie-authenticated unsafe-request Origin/Referer checks, response security headers, request correlation ID and a database-backed readiness probe.
- Bounded PostgreSQL connection pool with validated min/max/timeout settings.
- Dummy Argon2id verification for unknown-user login attempts.
- Concurrent OTP request serialization to prevent multiple simultaneous requests bypassing the phone ceiling.
- Exact idempotent replay behavior for superadmin wallet adjustments and provider payment events.
- Append-only PostgreSQL triggers for wallet ledger, reconciliation notes and administrator audit logs.

### Schema management and runtime privileges
- `scripts/manage_saas_migrations.py` applies ordered migrations with a PostgreSQL advisory lock, per-file SHA-256 history and one transaction per migration.
- It rejects changed or unknown migrations, checks the exact database name and refuses to infer the state of a non-empty schema without a migration ledger.
- It can grant DML/sequence privileges to a pre-created runtime role while withholding access to the migration ledger and schema creation.
- A separate migration-owner URL and a separate low-privilege application URL are required by the production Compose template.

### Automated evidence
- CI provisions PostgreSQL 16 and applies all SaaS migrations from a clean disposable database using the migration runner.
- PostgreSQL-backed integration tests cover authentication/session revocation, support-note isolation, cross-account ticket reads, every current staff/admin/analytics-admin GET route denied to a standard user, RBAC boundaries, analytics consent/deduplication/deletion, payment amount mismatch/event replay, fail-closed gateways, wallet concurrency/idempotency, OTP concurrency and append-only triggers.
- Production configuration tests require verified TLS for remote database connections; migration, backup and restore tools reject remote PostgreSQL connections without `sslmode=verify-full`.
- A second PostgreSQL 16 CI job provisions a separate read-only backup role (asserting it cannot INSERT/UPDATE/DELETE or CREATE), builds an age-encrypted custom-format backup, verifies its SHA-256, rejects remote backup/restore DSNs without verified TLS, decrypts/restores to a separate empty database and checks the restored schema.
- Security CI runs pip-audit, Bandit high-severity/high-confidence scanning, Python compilation, operational shell syntax checks, Docker build and production Compose configuration validation.
- Dependabot checks Python dependencies and GitHub Actions weekly.

### Deployment foundation
- `Dockerfile.saas` builds a Python 3.12 image running as UID/GID 10001.
- Production Compose uses a read-only root filesystem, tmpfs for temporary files, dropped capabilities, no-new-privileges, bounded PIDs and JSON log rotation.
- The API binds only to host loopback for a separately configured TLS reverse proxy. PostgreSQL is external and is not published by the Compose stack.
- Migration execution is a one-shot dependency; the API starts only after migrations succeed.

## Remaining mandatory release gates

1. **Prerequisite chain:** review and merge Phases 1–8 in the agreed order, then review Phase 9. They are currently separate Draft PRs; no production promotion has occurred.
2. **Existing DB migration adoption:** design and review a baseline procedure for any pre-existing SaaS database that was migrated before the checksum ledger existed. The runner deliberately refuses to guess its applied history.
3. **Complete authorization coverage:** test every route as anonymous, standard user, support, admin and superadmin. Verify cross-user access for all resource families, internal-note secrecy, analytics user history and every money mutation.
4. **Payment and money workflows:** obtain current official provider integration packs and sandbox credentials; complete signature verification, callback/retry/replay/timeout/concurrency tests, payment reconciliation, refund and bank payout workflows. Unconfigured providers must remain disabled.
5. **Rate limiting and perimeter security:** deploy tested IP/device/distributed rate limits, request body limits, WAF/firewall rules, proxy trust configuration, HTTPS-only ingress, secret rotation and monitoring. Application-level per-phone throttling alone is not sufficient.
6. **Independent application security:** complete independent code review, threat-model review and penetration test; remediate all critical/high findings and document any accepted residual risks.
7. **Backup and disaster recovery:** configure encrypted off-host backups, access restrictions, immutable retention, backup scheduling, alerting and measured RPO/RTO. Run a restore drill against the actual managed database/backup target.
8. **Monitoring and operations:** select a monitoring/logging provider; configure redacted structured logs, metrics, alert thresholds, on-call ownership and alert-delivery rehearsal. Run an incident-response tabletop and document results.
9. **Load and resilience:** run representative load, soak, concurrency and failure-injection tests against staging, using the actual connection budget, reverse proxy and provider sandbox. Agree service-level targets before testing.
10. **Deployment and rollback:** choose the production host, DNS, TLS, firewall, managed database, secret manager and rollback method. Run the Compose template in staging with a real TLS reverse proxy, least-privilege roles, backup and rollback validation. GitHub Pages alone cannot run the API or PostgreSQL.
11. **Privacy/legal review:** identify the operating entity, user locations, processing/storage/backup countries, subprocessors, cross-border transfers, retention/deletion obligations, payment/consumer rules and incident notification duties; obtain qualified advice for every applicable jurisdiction and publish reviewed terms/privacy notices.

## Exit checklist

- [x] Fresh-install SaaS migrations run on disposable PostgreSQL 16 in CI with checksum history.
- [x] Full Python suite: 171 passed in 8.83s on the verified combined Phases 8–9 branch (including PostgreSQL 16 integration tests).
- [x] Dependency audit, high-severity/high-confidence Bandit, shell syntax, Python compilation, non-root image build, Compose and Caddy validation pass on the verified Phase 9 head.
- [x] PostgreSQL-backed integration tests cover the explicitly listed critical flows.
- [x] Age-encrypted backup/restore script rehearsal passes on an isolated disposable PostgreSQL 16 cluster in CI, using a separately provisioned read-only backup role and explicit TLS negative tests.
- [x] TLS reverse-proxy and production Compose templates are validated in CI.
- [x] A bounded read-only staging probe is available with explicit non-production safeguards.
- [x] Dependency audit and high-severity/high-confidence SAST workflow pass on the verified CI head.
- [x] Non-root Docker image and hardened production Compose template are present and validated by CI.
- [ ] Existing production database baseline adoption is reviewed and tested.
- [ ] Complete route-level RBAC and object isolation matrix passes (standard-user denial coverage now includes all current staff/admin/analytics-admin GET routes; mutating-route and full role matrix still need additional coverage).
- [ ] Official provider sandbox, callback, refund and bank payout tests pass.
- [ ] Edge/IP/device/distributed rate limits and perimeter security are implemented and tested.
- [ ] Independent penetration/security review completed; findings remediated.
- [ ] Encrypted off-host backups, retention, measured RPO/RTO and production restore drill evidenced.
- [ ] Monitoring, alert routing and incident-response rehearsal completed.
- [ ] Staging load/soak/failure tests executed against a real approved staging host and results meet agreed targets.
- [ ] Load/soak/failure tests meet agreed service targets.
- [ ] Production host, TLS ingress, deployment and rollback process approved.
- [ ] Jurisdiction-specific privacy/legal review completed.

**Do not mark this phase production-complete or expose the SaaS service publicly until the remaining gates have evidence and owners.**
