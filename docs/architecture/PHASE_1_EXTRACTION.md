# Phase 1 — Backend internal structure: extraction slice 1

**Branch:** `refactor/architecture-stabilization-phase0`  
**Approach:** modular monolith; no separately deployed service is created in this slice.

## Delivered

- Added `site_api.errors.ApiError` as the single shared API exception.
- Moved the 27-case `/api/v1` route dispatcher into `site_api.dispatch.dispatch_api`.
- Kept `Handler._dispatch(method, path, params, body)` as a compatibility adapter with the same signature.
- Kept parsing, URL decoding, HTTP status serialization, content-type handling, static-file serving, all SiteApi methods and all SQL behavior in their existing locations.
- Added direct contract tests for the extracted dispatcher and a delegation test for the legacy Handler entry point.

## Compatibility guarantees

The dispatcher keeps the same:
- HTTP method and path matching rules;
- path parameter forwarding;
- query parameter and request body forwarding;
- JSON versus XLSX response content types;
- 404 behavior for unknown routes;
- 501 behavior and Persian error details for the two unimplemented admin mutations.

The existing API methods and database access were not moved or rewritten. The source market database and sidecar schema were not changed.

## Verification

The Phase 0 baseline CI run executed the full pre-extraction test suite: **88 passed**.

Phase 1 is considered complete only after CI passes on this extracted version too. Passing route-contract tests proves dispatcher parity for the inventory; it does not prove live database behavior, network deployment, load capacity or admin authorization. Those remain separate work items.

## Next extraction candidates

1. Extract the HTTP transport/static-file handler from `server.py` while retaining its external behavior.
2. Extract pure date/calendar helpers and domain calculations with characterization tests.
3. Separate sidecar projection refresh from request handling.
4. Only after those seams are stable, extract Market, Ownership, Portfolio and Analytics application modules.
