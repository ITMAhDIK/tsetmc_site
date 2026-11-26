# ADR-0001: Modular monolith before network microservices

- **Status:** Accepted
- **Date:** 2026-11-26
- **Context:** The current backend is a single 4,777-line standard-library HTTP server. Application methods share SQLite connections, sidecar projections, refresh coordination and domain helpers. Immediate network extraction would create service and data dependencies before module ownership is known.
- **Decision:** First extract cohesive Python modules inside the existing deployable application. Keep one process and preserve the `/api/v1` contract. Only split a module into a separately deployed service when it has a stable API, explicit data ownership, independent operational need, and contract/integration tests.
- **Consequences:**
  - Phase 1 is structural extraction, not a distributed deployment.
  - Existing frontend URLs and response semantics remain stable.
  - Shared in-process code is allowed temporarily, but dependency direction must be explicit.
  - Each extracted domain must have an owner and tests.
  - Network boundaries, timeouts, retries, authentication between services and observability are designed later, not invented prematurely.
- **Rejected alternative:** Split every current API prefix into a Docker container immediately. This would multiply deployment and failure modes while preserving unclear shared data dependencies.
