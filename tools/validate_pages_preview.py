"""Validate the static GitHub Pages preview without loading workbook data."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
INDEX = DOCS / "index.html"
DATA = DOCS / "data" / "analytics.json"


class DocumentCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_seen = False
        self.html_seen = False

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            self.html_seen = True

    def handle_data(self, data):
        if "ره‌نگار بورس" in data:
            self.title_seen = True


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    data_text = DATA.read_text(encoding="utf-8")
    data = json.loads(data_text)

    parser = DocumentCheck()
    parser.feed(html)
    if not parser.html_seen or not parser.title_seen:
        raise SystemExit("Static HTML structure/title check failed.")

    scripts = re.findall(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if len(scripts) != 1:
        raise SystemExit("Expected exactly one inline application script.")

    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required to syntax-check the dashboard script.")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as temp:
        temp.write(scripts[0])
        temp_path = temp.name
    try:
        subprocess.run([node, "--check", temp_path], check=True)
    finally:
        Path(temp_path).unlink(missing_ok=True)

    symbols = data.get("symbols") or []
    if len(symbols) != 6 or data.get("summary", {}).get("observations") != 180:
        raise SystemExit("Unexpected public analytics dataset dimensions.")

    forbidden = {
        "shareholder_id", "shareholder_name", "raw_source_id", "id_scheme",
        "holder_id", "share_id", "ondate_share_id", "canonical_name",
        "confirmed_positions", "probable_positions", "uncertain_candidates",
        "holding_value_rial", "shares_change", "identity_evidence",
    }

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key).lower()
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    leaked = forbidden.intersection(keys(data))
    if leaked:
        raise SystemExit("Private/raw data fields found: " + ", ".join(sorted(leaked)))

    if any(path.suffix.lower() == ".xlsx" for path in DOCS.rglob("*")):
        raise SystemExit("An Excel workbook must never be included in the public Pages folder.")

    print("Pages preview valid: HTML/JS syntax, JSON dimensions and privacy allow-list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
