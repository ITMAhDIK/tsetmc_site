# SaaS monitoring and alerting

Run the read-only schema/connectivity probe:

```bash
SAAS_DATABASE_URL='postgresql://...' python tools/saas_healthcheck.py
```

It checks connectivity and critical objects from migrations 001–004, without
returning account data or connection details. A database probe does not prove
end-to-end login, SMS, payment provider or browser availability.

| Area | Signals | Example alert |
|---|---|---|
| API | Availability, latency, 5xx | 5xx >2% for 5 minutes |
| Auth | OTP rejects, rate limits, login anomalies | sustained spike |
| Staff | MFA failures, role changes, session revocations | every superadmin change |
| Payments | pending orders, callback errors, reconciliation age | older than provider SLA |
| Wallet | ledger mismatch, negative available cash | any invariant violation |
| Withdrawals | pending age, missing payout references | queue over operating SLA |
| PostgreSQL | pool saturation, slow queries, backup status | >80% pool or missed backup |
| Jobs | outbox age, retry/dead-letter count | growing backlog |
| Analytics | consent, rejected events, retention lag | any collection without consent |

Do not place OTPs, tokens, phone/email, payment payloads or ticket contents in
metrics or alerts. Configure an external monitoring provider, metrics exporter,
paging destination and retention on the selected host; none is provisioned here.
