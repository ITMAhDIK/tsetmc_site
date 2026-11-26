# SaaS incident response playbook

## Severity
- **SEV-1:** broad account takeover, payment/wallet integrity loss, exposed
  credentials or sensitive-data disclosure.
- **SEV-2:** partial auth/payment outage, suspected staff compromise, queue
  failure or consent breach.
- **SEV-3:** isolated user issue or recoverable non-critical degradation.

## First response
1. Assign an incident commander and open a private incident record.
2. Record UTC time, affected systems/data, detection source and evidence.
3. Preserve logs, audit records, provider event IDs and backup manifests.
4. Determine whether SaaS and market-data domains are independently affected.

## Containment
- Revoke sessions and rotate exposed secrets through the secret manager.
- Disable compromised staff accounts; preserve the last active superadmin.
- Pause payment/referral/withdrawal jobs if integrity is uncertain. Never mark
  payments as paid manually or edit append-only wallet entries.
- Disable analytics ingestion if consent or collection behavior is uncertain.
- Restrict gateway ingress; hiding frontend navigation is not a security fix.

## Recovery and closure
Restore only to the SaaS DB URL after verifying archive checksum and approval.
Check schema readiness, user access, entitlements, provider settlement, cash/gift
balances, withdrawal reservations and audit history. Re-enable integrations
gradually after smoke tests. Notify users/providers/regulators as contract and
law require. Document root cause, impact, actions, owners and due dates; hold a
blameless review within five business days.

This template is not jurisdiction-specific legal advice.
