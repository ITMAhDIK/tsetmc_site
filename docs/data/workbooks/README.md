# Investor workbooks

Upload the 14 per-symbol Excel workbooks supplied for the investor-history and sector-affinity preview into this folder.

The intended static Pages pipeline is:
1. Inspect workbook sheet names and headers.
2. Normalize dated shareholder observations without merging identities by display name.
3. Generate a static investor index and historical observations JSON.
4. Render each investor's observed symbol history and sector-affinity heatmap from that JSON.

Do not upload SQLite databases, credentials, or unrelated workbook exports here.
