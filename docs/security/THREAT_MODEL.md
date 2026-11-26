# Tloomberg SaaS threat model

Status: engineering baseline; independent security review required.
Scope: SaaS identity, subscriptions, payments, wallet, referrals, support,
product analytics and staff console. Market data remains a separate trust and
storage domain.

## Assets and trust boundaries

Critical assets include verified identities/sessions, roles and entitlements,
payment callbacks/orders, cash/gift ledger and withdrawals, private tickets,
analytics consent/events, admin audit logs/backups, and the market Sidecar.

The browser/GitHub Pages is untrusted. The SaaS API must validate sessions,
CSRF/Origin, request schemas, rate limits and server-side permissions. SaaS
PostgreSQL and market-data storage require independent credentials and backups.
SMS, OAuth and payment providers remain untrusted until signatures, state,
issuer/audience/nonce and server-to-server verification succeed.

## Threats and required controls

| Threat | Control | Status |
|---|---|---|
| Forged admin role | Server-derived permissions; ignore browser role fields | Phase 8 domain checks; live adapter pending |
| Session theft/fixation | Secure HttpOnly SameSite cookies, rotation, revocation and CSRF | Contract only; auth service pending |
| OTP abuse | Hashed single-use OTP, expiry, attempt limits and rate limits | Schema/contract only |
| Payment replay/spoofing | Signed callback, provider verification, unique event IDs and idempotency | Schema/policy; gateway integration pending |
| Wallet double spend | Row locks, append-only ledger, cash/gift split and reservations | Domain tests |
| Privilege escalation | Least-privilege roles, superadmin-only elevation, last-admin guard, audit | Phase 8 domain service; MFA pending |
| SQL injection | Parameterized values, allow-lists and bounded pagination | Tests cover screener/admin code |
| Analytics over-collection | Opt-in, event allow-list, no free text/form values, retention | Phase 7 service/tests |
| Ticket leakage | Internal-note flag and staff-only access; no ticket text in analytics | Schema/domain controls |
| Cross-domain compromise | Separate DB URLs, credentials and backup targets | Architecture and ops tools |
| Backup theft | Private files, encryption at rest/in transit, restricted access | Local mode controls; offsite encryption pending |
| Denial of service | Rate limits, bounded payloads, pool limits, monitoring and staging load probe | Bounds/probe; production gateway config pending |

## Non-negotiable launch gates

- Do not expose legacy market `/api/v1/admin/*` routes as SaaS staff APIs; the
  baseline dispatcher has no authenticated session argument.
- Do not use UI-only demo auth or client-side role hints as security controls.
- Require secure session validation, server-side permissions, CSRF/Origin,
  MFA for privileged staff, short session lifetimes and revocation on role/status changes.
- Payment activation may only follow verified provider settlement.
- Analytics consent defaults off; no collection before notice and withdrawal UI.
- Keep backups encrypted and restore-test them in an isolated database.
- Keep secrets in a host secret manager, never Git, HTML, Pages config or argv.

This is not a penetration test or legal opinion. A qualified reviewer must
validate implementation and jurisdiction-specific privacy/payment duties.
