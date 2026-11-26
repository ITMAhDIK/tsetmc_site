# Staging-only read load probe

`tools/readonly_load_test.py` sends GET requests only and allows only HTTPS
hostnames containing `staging` with paths `/health/ready` or
`/api/v1/market/overview`. It caps each run at 1,000 requests and 10 workers
(default 100 requests, concurrency 5). An optional bearer token is read only
from `STAGING_READONLY_BEARER_TOKEN`.

```bash
python tools/readonly_load_test.py --target-url https://api-staging.example.com/health/ready --environment staging --ack-staging-traffic STAGING-ONLY --requests 300 --concurrency 5
```

Do not run on production or on login, payment, referral, wallet, withdrawal or
mutation routes. Coordinate a window, start low, monitor API/DB metrics and stop
if latency/errors rise. This measures HTTP responsiveness only, not financial
transaction correctness or production capacity.
