# Read-only ownership source-data audit

## Purpose

tools/audit_ownership_source.py performs a first-pass integrity audit of the raw TSETMC holdings database. It is intended to establish evidence before changing the ownership resolver. It is independent of the site Sidecar and does not rebuild or modify either database.

## Run

From the repository root on Windows PowerShell:

```powershell
python -m py_compile tools/audit_ownership_source.py tests/test_audit_ownership_source.py
python -m pytest -q tests/test_audit_ownership_source.py
python tools/audit_ownership_source.py --db "<FULL_PATH_TO_tsetmc_holdings.sqlite>" --sample-limit 50 --output "ownership_source_audit.json"
```

Replace the database placeholder with the exact source DB path. The script opens the source database with SQLite mode=ro and enables PRAGMA query_only=ON. The optional --output path writes only the JSON report; it does not write to the database.

## Checks

1. **Duplicate source keys** — multiple rows for the same (ins_code, date, id_scheme, shareholder_id), including whether their share quantities disagree. These are investigation candidates, not automatic deletion candidates.
2. **Issued-share denominator conflicts** — multiple positive total_company_shares values within one (ins_code, date) in company_daily or shareholder_holdings.
3. **Denominator-source disagreement** — company_daily and holdings rows carry different positive totals for the same instrument/date.
4. **Raw totals above 100%** — sums raw positive shares before identity resolution and source-exclusion logic. This is a diagnostic, not the resolved ownership result.
5. **Reported percentage versus same-date recomputation** — compares ownership_percent with shares / company_daily.total_company_shares * 100 on the exact same instrument/date. Differences over 0.01 percentage points are sampled.
6. **Pipeline-calculated percentage versus same-date recomputation** — same comparison for ownership_percent_calc. Historical differences may expose a stale/current denominator in the source pipeline; they are not silently rewritten.
7. **Identity cross-reference integrity** — duplicate source keys, multiple canonical targets for one source key, and differing share quantities among members mapped to the same canonical identity on the same date.

Every check reports a total count and up to --sample-limit examples. Missing optional percentage columns or the xref table are explicitly reported as skipped.

## Interpretation and limits

- This is a source-data diagnostic only. It does not reproduce the full resolver, prove two rows are the same person, or decide which conflicting quantity is correct.
- A raw total above 100% may be explained by cross-scheme duplicates or repeated endpoint rows; it must be compared with the source-exclusion evidence and resolver output.
- MAX(shares) and MAX(total_company_shares) are reported as potential conflict-handling risks by the audit samples; the audit does not endorse either as the correct value when source rows disagree.
- The script does not contact TSETMC, open the site Sidecar, run the server, rebuild summaries, or change the frontend.
- A clean result is not a production-readiness approval. It is one evidence layer before a separate resolver/Sidecar parity audit.