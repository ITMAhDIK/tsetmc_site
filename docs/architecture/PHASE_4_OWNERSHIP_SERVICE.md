# Phase 4 — Ownership Service extraction

Branch: `refactor/architecture-stabilization-phase0`

## Delivered

Ownership read operations were moved into
`site_api.ownership_service.OwnershipService`:

- Single-symbol shareholder snapshot and same-date identity resolution
- Identity-resolved ownership percentage/share time series
- Shareholder change events and their value fallback to the latest valid close
- Cross-company shareholder search and person-level identity grouping
- Per-shareholder observation history

The date-row identity resolver is now a module-level function in the Ownership
Service package. `SiteApi._resolve_date_rows` remains as a compatibility adapter
for pre-existing internal callers and regression tests.

## Shared identity boundary

The batched majority-twin map remains in `SiteApi` because both the ownership
search path and later portfolio operations use it. The Ownership Service receives
it via dependency injection; it does not duplicate or reinterpret the identity
proof rules.

The service also receives its Sidecar/live connection factories, readiness
check, symbol resolver, company-daily lookup, date converters and price helper
through constructor injection. Source holdings are still read-only.

## Validation improvement found by integration tests

The cross-phase integration suite exposed that the arithmetic Jalali converter
accepted out-of-range month/day values and rolled them into another month.
`jalali_to_gregorian_date` now validates month lengths and Esfand against the
existing 33-year leap rule before conversion. Tests cover invalid months, invalid
day ranges, a non-leap Esfand 30 and a valid leap-year Esfand 30.

## Verification

`tests/test_ownership_service.py` exercises the actual `SiteApi` composition
root using disposable SQLite files. It covers shareholder snapshots, raw versus
resolved totals, series percentages, event valuation, cross-company identity
groups, observation history, readiness errors and versioned dispatcher routing.
Existing representative-selection tests continue through the compatibility
adapter.

This is an internal modular-monolith service boundary, not an independently
deployed network microservice.
