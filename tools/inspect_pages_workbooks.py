#!/usr/bin/env python3
"""Create a privacy-conscious schema manifest for uploaded investor workbooks.

Only workbook filenames, sheet dimensions, and candidate header names are
written. No shareholder IDs, names, or row values are copied into the manifest.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_DIR = ROOT / "docs" / "data" / "workbooks"
OUTPUT = ROOT / "docs" / "data" / "workbook_manifest.json"


def clean_header(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def inspect_sheet(ws) -> dict[str, Any]:
    # Detect a likely header row among the first 30 rows. Prefer the earliest
    # row among ties, which is typical for the supplied per-symbol exports.
    best_row = 1
    best_values: tuple[Any, ...] = ()
    best_count = 0
    scan_rows = min(max(ws.max_row or 0, 1), 30)

    for row_number, values in enumerate(
        ws.iter_rows(min_row=1, max_row=scan_rows, values_only=True), start=1
    ):
        cleaned = tuple(clean_header(v) for v in values)
        count = sum(v is not None for v in cleaned)
        if count > best_count:
            best_row = row_number
            best_values = cleaned
            best_count = count

    headers = [
        {"column_index": index + 1, "name": name}
        for index, name in enumerate(best_values)
        if name is not None
    ]
    return {
        "sheet_name": ws.title,
        "max_row": ws.max_row,
        "max_column": ws.max_column,
        "candidate_header_row": best_row,
        "headers": headers,
        "estimated_data_rows": max(0, (ws.max_row or 0) - best_row),
    }


def main() -> int:
    files = sorted(WORKBOOK_DIR.glob("*.xlsx"))
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "workbook_count": len(files),
        "workbooks": [],
        "note": (
            "Schema inspection only. No shareholder IDs, investor names, or "
            "row-level values are included."
        ),
    }

    for path in files:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            manifest["workbooks"].append({
                "file": path.name,
                "size_bytes": path.stat().st_size,
                "sheets": [inspect_sheet(ws) for ws in workbook.worksheets],
            })
        finally:
            workbook.close()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if not files:
        print("No .xlsx files found yet; uploaded workbook folder is empty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
