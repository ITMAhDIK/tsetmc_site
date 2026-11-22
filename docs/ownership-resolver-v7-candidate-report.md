# v7 ownership resolver candidate — implementation record

## Branches
- Snapshot overlay base: `snapshot/v7-recorded-2026-11-21`.
- Candidate: `candidate/v7-ownership-resolver-2026-11-22`.
- Review-only draft PR targets the two-file v7 snapshot overlay branch, not `main`.
- PR #1 is intentionally unchanged.

## Provenance and limitations
- Input snapshot: v7 captured 2026-11-21 20:45 from `D:\\tsetmc_site`.
- `server.py` source SHA-256: `3ad3e3386706d06a89c571f9cd2ac5be019c4f8cdc3af362e07ec038ccde992b`.
- HTML source SHA-256: `cf5fcf9d9b9ff13a00cd120d8a723819fb05eda3830151f98c6287d74313f3f1`.
- Live-path hashes have not been re-verified because the tunnel is unavailable.
- The snapshot overlay is **not a full historical repository export**: it was branched from the current GitHub `main` tree and only the two application entry files (`server.py` and `ai_studio_code.html`) were replaced with the recorded v7 files. Other repository files remain from `main` and must not be assumed to match the v7 workstation checkout.
- The snapshot source files were normalized from CRLF to LF for Git storage.
- Candidate package path `current/ai_studio_code.html` was mapped to repository-root `ai_studio_code.html`; that is the HTML path present on the repository.

## Implemented
- `open_sidecar_db` uses `uri=True` for read-only URI attachment compatibility.
- v5 date-scoped ownership resolver and `resolved_*` output fields are ported onto v7.
- v7 source-proven duplicate exclusions (`site_excluded_holder_rows`) are preserved and excluded before resolver grouping.
- Same-date denominator is required; unavailable/invalid denominator does not fall back to raw or stale percentages.
- Out-of-range totals remain review-required; values are not silently clamped.
- Frontend consumes resolved groups and does not merge holders by name.
- `SIDECAR_VERSION` remains 7. `ownership_resolver_schema=resolved-v1-source-exclusions` forces rebuild when absent; failed resolver/enrichment must not leave a successful `last_refresh` marker.

## Validation status reported for this candidate
- `py_compile`: pass.
- Three focused `unittest` checks: pass.
- SQL stage-8 flow on synthetic SQLite fixtures: pass.
- `uri=True` ATTACH smoke test: pass; this is not Windows validation.
- JSX/JavaScript parser diagnostics: zero.
- Full project test suite, Windows validation, full-database performance, live hash verification, canary, and before/after checks for احیا / احیا۳ remain pending.
- Focused tests were run in the candidate preparation environment; they are not added as a standalone test suite in this GitHub PR.

## Deterministic representative-row tie-break (2026-11-22)

A follow-up review found that two members of one canonical holder could have the same best identity tier and the same normalized name while carrying different display fields (for example, shares change, price, or market value). The Python endpoint previously retained whichever row happened to arrive first.

Candidate-only correction:
- Keep identity-tier rank as the primary selection rule.
- Keep `name_key` as the first tie-break, preserving parity with the step-8 SQL naming rule.
- For equal rank and equal `name_key`, use a stable tuple of the remaining display fields so selection is independent of SQL row order.
- Compare numeric display fields numerically with `Decimal`, not lexicographically as strings. Numeric strings are parsed as numbers; `None`, malformed, and non-finite values have explicit deterministic ordering. Text fields distinguish `None` from an empty string.
- This is only a deterministic fallback; it does not establish which conflicting source value is factually correct.
- Expanded `tests/test_v7_representative_tie_break.py`, using an in-memory SQLite fixture only. It verifies forward/reverse input-order parity, multi-digit numeric values (9 vs 10), numeric strings, negative and decimal values, missing/malformed/non-finite numeric values, `None` versus empty text, and unchanged same-date ownership calculation.

The typed-key correction and expanded tests are committed to the candidate branch. Local execution is still required before considering the PR ready. No production database, sidecar, deployment, or live service is required for these tests.

## Operational restrictions
- No merge, rebase, deploy, canary, production DB open/write, or sidecar rebuild has been performed.
- Do not connect this candidate to the production database or Sidecar. A legacy Sidecar requires rebuild due to the resolver schema marker.
- Any rebuild/canary requires a separate impact audit, backup, and explicit approval.

## Additional readiness audit finding (2026-11-22)

A static review found that `require_ready()` originally checked only the legacy `last_refresh` marker. If a rebuild failed before the resolver's own failure guard (for example, during source-exclusion collection), an old marker could survive and permit API handlers to treat a stale/partial sidecar as ready.

Correction committed to this candidate:
- `refresh_summaries()` now deletes both `last_refresh` and `ownership_resolver_schema` before the first rebuild operation.
- `require_ready()` now requires both a completion timestamp and the exact `resolved-v1-source-exclusions` marker.
- Added `tests/test_ownership_resolver_readiness.py` with four regression tests covering valid readiness, legacy/mismatched schema rejection, and early rebuild failure.

Validation state for this additional correction:
- Source and test file are committed to this PR branch.
- The four new tests have **not yet been executed** because the isolated local execution environment was unavailable during this review.
- They must be run with the full project suite by the user in their own local shell before the PR is considered ready.
