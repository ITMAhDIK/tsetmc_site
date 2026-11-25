# Investor sector affinity

## Signals

The portfolio page computes three distinct measures from the existing identity-resolved disclosure history:

1. Current allocation: same-date disclosed shares multiplied by a positive reference/closing price. No balance is carried forward to a later disclosure date.
2. Historical affinity: exponentially decayed average of observed sector allocations. The default half-life is six months.
3. Recent net rotation: signed change in disclosed share quantities multiplied by the observation-date reference price. Only consecutive observations without corporate-action or anomaly markers qualify.

The interest score is a transparent weighted combination: 50% current allocation, 30% historical affinity and 20% recent-rotation score. The rotation component is neutral at 50 when there is no eligible evidence. Positive or negative net changes move it above or below 50 using a five-percentage-point scaling constant. A confidence score and data-quality counts accompany every result.

## Official groups and dashboard categories

The official keys are the TSETMC codes and labels cSecVal / lSecVal and cSoSecVal / lSoSecVal. They are the calculation dimensions and are never inferred from a symbol.

The file data/dashboard_sector_categories.json is a separate presentation layer:

- official_group_to_category: maps a cSecVal code to a dashboard category id.
- official_subsector_to_category: maps cSecVal:cSoSecVal to a dashboard category id. A subgroup mapping takes precedence over a group mapping.
- categories: list of category ids and display labels.

The empty starter mappings are intentional. Add mappings only after reviewing the current official registry. The application does not guess categories from labels or code prefixes.

## Build the official instrument registry

Run from the repository root on a machine that can reach TSETMC:

```powershell
python tools/sync_instrument_sectors.py --db "D:\path\tsetmc_holdings.sqlite"
```

The script opens the source database read-only, reads instrument codes from companies, requests GetInstrumentIdentity/{insCode}, and writes data/instrument_sectors.json atomically. Existing successful records are skipped, so a stopped run can be resumed. Use --limit 20 for a first connectivity test and --force to refresh every instrument. Failed requests are logged and retried; they are not silently converted into a made-up group.

Review the official codes and labels, then edit data/dashboard_sector_categories.json to define the dashboard groups. Do not hand-edit the generated instrument registry.

## Performance and data safety

- Reuses the existing indexed site_holder_history identity/date stream. It does not scan the raw holdings table for each investor request.
- Accumulates sector metrics during the same pass that builds the historical portfolio summary; no second historical query is needed.
- Buffers only one disclosure date at a time.
- Makes no changes to the source SQLite database and does not change the sidecar schema.
- Collapses CISIN aliases to one economic position per date. Conflicting alias quantities or valuations are excluded.
- Uses the existing linked identity resolver. Direct records and verified links receive stronger evidence than inferred links; name-only identity matching is not used.
- Missing quantities, missing/stale/future prices, identity conflicts, unclassified values, gaps, corporate actions and anomalies remain visible in the data-quality report or are excluded from net-rotation attribution.

## Interpretation limits

The source is periodic shareholder disclosure, not a transaction tape. A net increase/decrease does not prove a buy/sell execution. A missing disclosure is not treated as zero. Corporate actions are not interpreted as investor trading. Values are estimates based on reference prices and may not match actual execution prices.

The history series is observed-value history, not a continuous daily NAV: balances are not carried forward between disclosure dates. Disclosure may also be partial on any given date, so the latest observed instrument count and confidence/coverage values must be read alongside the allocations.

## Validation

Run the repository tests with:

```powershell
python -m pytest -q
```

Synthetic regression tests cover allocation, recency weighting, price-only moves, signed net changes, corporate actions, gaps, anomalies, identity conflicts, CISIN aliases, missing/stale/future prices and unmapped groups.

A live validation still requires running the registry sync and checking selected real investors across several dates against their underlying disclosure ledgers. The score should not be treated as calibrated until this sample review is complete.
