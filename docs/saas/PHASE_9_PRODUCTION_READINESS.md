# Phase 9 — Production readiness

## Purpose and release posture

Phase 9 is a release-readiness programme, not a feature launch. Its exit criteria apply to the complete dependency chain (Phases 1–8), not just the code introduced in this phase.

**Current posture: NOT READY FOR PUBLIC PRODUCTION.** The existing phase branches are still Draft and unmerged. No production SaaS database, hosting environment, payment/SMS/bank credentials, backup store, monitoring destination, legal entity, or target jurisdiction has been supplied or verified. This document records engineering controls and evidence still required; it does not certify compliance or operational readiness.

## System boundary and assets

| Component | Trust boundary / responsibility | Data or privilege to protect |
|---|---|---|
| Browser and GitHub Pages | Untrusted client; static presentation only | Session cookie, user input, analytics consent |
| SaaS FastAPI service | Public-facing application boundary; must be deployed separately from Pages | Authentication, authorization, payment state, wallet mutations, support and admin actions |
| Dedicated SaaS PostgreSQL | Primary SaaS persistence boundary | Identity, sessions, phone data, payment and wallet ledgers, support messages, analytics, audit records |
| Payment/SMS/bank providers | External systems; no trust without authenticated provider responses | Payment references, OTP delivery, bank-account vault references |
| GitHub repository and Actions | Source and build supply-chain boundary | Source integrity, workflow permissions, secrets and deployment credentials |
| Market-data collector/database/API | Separate product boundary | Must not be connected to or modified by SaaS operational tooling |

Sensitive assets include password hashes, session-token digests, OTP challenge hashes, phone numbers, encrypted notification payloads, payment provider references, wallet ledger entries, support conversations/internal notes, analytics identifiers, bank-vault tokens and administrator audit logs.

## Threat model and controls

| Threat / abuse case | Existing or planned control | Evidence still required before release |
|---|---|---|
| Credential stuffing, password guessing and OTP abuse | Argon2id password hashing; OTP HMAC; one-time expiring challenges; per-phone OTP request ceiling and attempt counter | Distributed rate limits, IP/device abuse controls, alerting, load tests and account-recovery review |
| Session theft or browser request forgery | Opaque random sessions; only token digests stored; HttpOnly/SameSite cookie; Secure defaults to true | HTTPS-only deployment, explicit origin/CSRF strategy, cookie-domain review, session rotation and revocation integration tests |
| Privilege escalation / broken object-level authorization | Server-side permissions and user-scoped repository methods; superadmin console requires separate console and action permissions | Full positive/negative RBAC matrix against PostgreSQL, cross-user isolation tests and independent code review |
| Payment callback forgery, replay or amount mismatch | Server-side verification contract, amount/reference validation, provider-event deduplication; unavailable adapters fail closed | Official provider integration documents, signature verification, sandbox and replay/concurrency tests |
| Wallet double-spend or unauthorized adjustment | Append-only ledger, idempotency keys, row locking and negative-balance checks in application logic | Concurrent transaction tests, reconciliation invariants and restore/replay rehearsal |
| Withdrawal approval mistaken for payment | Admin review is separate from bank payout; no manual paid action or payout endpoint | Bank provider contract, dual-control policy, reconciliation and evidence of payout settlement |
| Disclosure of support notes or analytics details | Separate internal-note filtering; consent-gated analytics; small-cohort suppression; separate user-history permission | PostgreSQL access-control tests, privacy review, retention execution evidence and output inspection |
| SQL injection or unsafe dynamic SQL | Parameterized database calls and allow-listed sort/filter inputs in current service code | Automated SAST, manual review of every query, dependency scan and adversarial input tests |
| Database loss, ransomware or operator error | Backup and isolated restore-verification scripts added in this phase | Encrypted off-host backup storage, immutable retention, scheduled runs, restore drill and documented RPO/RTO |
| CI/dependency compromise | Least-privilege security workflow; dependency audit and high-severity/high-confidence Bandit scan | Pin critical third-party actions by commit SHA, review workflow changes, secret scanning and remediation SLAs |
| Insider misuse or compromised administrator | Audited privileged mutations, out-of-band superadmin bootstrap/removal | Independent audit-log review, alerting, access recertification and dual approval for high-risk money operations |

This is a working threat model, not a completed penetration test. The risk list must be revisited when the hosting topology, provider integrations and data jurisdictions are known.

## Phase 9A delivered in this branch

- Security baseline workflow: dependency vulnerability audit plus high-severity/high-confidence Bandit analysis on pull requests, feature/main pushes, weekly schedule and manual dispatch.
- Additional unit tests for password-hash policy, opaque session token digests, OTP HMAC secret enforcement/domain separation and E.164 normalization.
- PostgreSQL backup helper: custom-format backup, explicit expected-database-name guard, private file permissions, archive readability check and SHA-256 sidecar. It refuses to place backups inside the checked-out Git repository.
- Isolated restore-verification helper: checksum validation and restore into an explicitly named, pre-created, empty verification database; it rejects a destination matching the configured production database name and never drops a database.
- Operations and incident-response runbook.

These scripts have syntax checks in CI. **They have not been run against a real PostgreSQL instance in this environment.** They are not a substitute for encrypted off-host storage, managed backup retention, monitoring or a restore drill.

## Mandatory release gates

A public release remains blocked until each gate has an owner and evidence:

1. **Database and migrations:** apply every migration to a disposable PostgreSQL instance; verify clean install and upgrade paths; test constraints, triggers, indexes and rollback behavior.
2. **Authorization and isolation:** exercise every route as anonymous, standard user, support, admin and superadmin; prove cross-user reads/writes are denied; verify internal support notes and user-level analytics cannot leak.
3. **Payments and money:** test provider signatures, amount/currency/reference matching, duplicate callbacks, delayed callbacks, retries, concurrent checkout/settlement, wallet invariants, withdrawals and reconciliation against official sandbox environments.
4. **Application security:** complete independent threat-model review and penetration test; close all critical/high findings; implement and test CSRF/CORS, rate limiting, request-size limits, security headers, secret rotation and production session settings.
5. **Backup and recovery:** schedule encrypted off-host backups; restrict backup access; define RPO/RTO; run a restore drill into an isolated database; verify row counts and business invariants; record recovery duration.
6. **Monitoring and operations:** add redacted structured logs, request correlation, service/database health, latency and error metrics, alerts, on-call ownership and alert rehearsal. Never log passwords, OTP values, session tokens, provider secrets or bank-vault tokens.
7. **Performance and resilience:** run representative load, soak, concurrency and failure-injection tests; identify database pool/connection limits and recovery behavior.
8. **Deployment and rollback:** select and harden a Python-capable host; configure HTTPS, firewall, restricted DB role, secrets manager, migration job and tested rollback. GitHub Pages alone cannot host the API or PostgreSQL.
9. **Privacy and legal:** identify controller/operator entities, user locations, processing and storage countries, cross-border transfers, retention/deletion obligations, consumer/payment obligations and provider contracts; obtain qualified advice for every applicable jurisdiction and publish reviewed terms/privacy notices.

No deployment workflow is enabled by this phase. A deployment target, credential model, environment ownership and rollback contract must be selected before automating production changes.

## Exit checklist

- [ ] All prerequisite phase PRs reviewed and merged in order.
- [ ] Dedicated SaaS PostgreSQL integration suite and migration tests pass.
- [ ] RBAC, tenant isolation, internal-note and analytics privacy tests pass.
- [ ] Provider sandbox and money-concurrency tests pass.
- [ ] Independent security review and penetration test completed; findings remediated.
- [ ] Dependency and SAST scans clean or have explicitly accepted, time-bounded exceptions.
- [ ] Encrypted off-host backups and isolated restore drill evidenced.
- [ ] Monitoring, alert routing and incident response rehearsal completed.
- [ ] Load/soak/failure tests meet agreed service targets.
- [ ] Production host, deployment and rollback process approved.
- [ ] Jurisdiction-specific privacy/legal review completed.
