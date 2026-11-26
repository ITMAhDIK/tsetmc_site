# SaaS monitoring and alerting

## Metrics and access boundary

The FastAPI service exposes `GET /internal/metrics` in Prometheus exposition
format. It requires the dedicated `SAAS_METRICS_TOKEN` bearer credential.
Generate at least 32 random characters and keep the value separate from the
database, OTP, outbox-encryption, and MFA-encryption secrets.

The metric labels are deliberately low-cardinality and contain only the HTTP
method, registered route template, and response status. Request IDs, cookies,
tokens, IP addresses, user IDs, phone numbers, payloads, query strings, and
raw URL paths are not exported.

Caddy proxies only `/api/v1/*`; it does not proxy `/internal/metrics`. The
optional Prometheus service runs in the private Compose network and has no
published host port. Access the Prometheus UI through an authenticated
administrative tunnel or a separately approved private ingress; do not publish
port 9090 to the public Internet.

## Enable the bundled private Prometheus service

1. Create a random metrics bearer token and configure the same value as
   `SAAS_METRICS_TOKEN` in the protected API environment.
2. Store that value, without the `Bearer ` prefix, in
   `SAAS_METRICS_TOKEN_FILE` with owner restricted to the deployment operator
   and mode `0600`.
3. Validate the Compose configuration, then start the API and monitoring
   profile with the approved immutable image digest:
   `docker compose -f docker-compose.saas.production.yml --profile monitoring up -d`.
4. Verify the target is UP in Prometheus and confirm the
   `SaasApiTargetDown`, `SaasApiHigh5xxRate`, and `SaasApiHighP95Latency`
   rules are loaded.

The API uses Prometheus multiprocess collection under
`PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus`; the directory is initialized on
container start and is held in the API container's private tmpfs.

## Alert delivery and host monitoring

The bundled rules produce Prometheus alerts but do not configure a real
Alertmanager receiver. Before go-live, connect Alertmanager to an approved
on-call destination, configure ownership and escalation, and run a notification
test. Also configure host-level CPU, memory, disk, filesystem, container
restart, PostgreSQL capacity, backup age, backup success, and TLS-certificate
expiry alerts in the chosen hosting provider.

A dashboard or green Prometheus target does not demonstrate that alerts reach
a human. Capture the receiver test and an incident-response exercise as
release evidence.

## Privacy and retention

The HTTP metrics contain only route-level aggregates. Prometheus storage is
configured for a 15-day local retention period by default. Adapt retention and
storage capacity to the approved operations policy; do not place raw request
bodies or identifiers in labels.

This configuration is a deployable template. It is not evidence that a host,
Prometheus instance, Alertmanager, notification provider, or on-call rotation
has been provisioned.
