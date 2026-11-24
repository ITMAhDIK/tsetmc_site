# Historical shareholder portfolio

## Purpose

The investor portfolio includes every security observed for the investor across the available dated shareholder disclosures. A security remains in the historical list even when it is absent from the latest disclosed shareholder list.

## Identity

History is queried through the existing identity resolver. The resolver's linked `(id_scheme, shareholder_id)` pairs are used; names alone are never used to merge investors. If linked IDs produce conflicting quantities for the same security and date, the observation is marked as an identity conflict and excluded from valuation.

## Observation and change semantics

- The first observation is the first available disclosure record, not a proven purchase date.
- A missing or null quantity is displayed as `not_disclosed`; it is not converted to zero.
- Quantity increases and decreases are net changes between observations, not proof of executed buys or sells.
- If a disclosure gap separates two valid quantities, the change is marked as occurring after a gap; the implementation does not pretend the observations were consecutive.
- A corporate-action marker takes precedence over a simple increase/decrease classification when the source provides that marker.
- Reference prices are source prices or the latest valid exchange close on or before the observation date. They are not execution prices.
- Portfolio value on a date includes only positions with a valid quantity and reference price on that date. Old balances are not carried forward across dates with no disclosure.

## Assumed-sale convention

The product deliberately uses an **assumed sold** label when a later `company_daily` snapshot exists for a security after the investor's last disclosed observation in that security.

This is a presentation convention requested for portfolio reconstruction, not proof of a transaction. The investor may simply have fallen below TSETMC's disclosure threshold. The UI must keep this caveat visible anywhere the assumed-sale label is shown. If no later company snapshot exists, the exit status remains unknown; if the investor is present on the latest company snapshot date, the status is currently disclosed.

## API

`GET /api/v1/portfolios/{id}/history?ins_code={ins_code}&as_of_date={YYYY-MM-DD}&limit=100&offset=0`

- `id` is `id_scheme:shareholder_id`.
- `ins_code` selects one security's chronological observation ledger.
- `limit` is bounded to 1–500; `offset` is bounded and defaults to zero.
- The endpoint streams observations and returns only the requested page plus the total count.

The portfolio detail endpoint also returns `historical_summary`, `historical_symbols`, and an observed-value `history_series`.

## Sidecar and audit

The history table is a rebuildable derived table in the site sidecar. The source holdings database remains read-only. The sidecar layout version is 10; after deploying this code, the server's normal refresh must rebuild the sidecar before the new API is ready.

Run the read-only audit after the refresh:

```powershell
python tools/audit_portfolio_history_sidecar.py --db "D:\\path\\tsetmc_holdings.sqlite" --sidecar "D:\\path\\site_data.sqlite"
```

The audit compares grouped source keys with the materialised history table, checks the sidecar version and completion marker, and does not modify either database.
