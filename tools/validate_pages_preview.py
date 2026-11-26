"""Validate the full local-first UI published as a static GitHub Pages preview."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
INDEX = DOCS / "index.html"
DATA = DOCS / "data" / "analytics.json"
ANALYTICS_CLIENT = DOCS / "analytics-client.js"

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
    data = json.loads(DATA.read_text(encoding="utf-8"))
    parser = DocumentCheck()
    parser.feed(html)
    if not parser.html_seen or not parser.title_seen:
        raise SystemExit("Static HTML structure/title check failed.")

    scripts = re.findall(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",
        html, flags=re.IGNORECASE | re.DOTALL,
    )
    if len(scripts) != 1:
        raise SystemExit("Expected exactly one inline React/Babel application script.")
    if 'type="text/babel"' not in html or 'data-presets="react"' not in html:
        raise SystemExit("Full React UI must use the configured Babel React preset.")
    if not (DOCS / "ui-refresh.css").is_file():
        raise SystemExit("The full UI stylesheet is missing from the Pages artifact.")

    if not ANALYTICS_CLIENT.is_file():
        raise SystemExit("The first-party analytics client is missing from the Pages artifact.")
    if not re.search(
        r"""<script[^>]+src=["']\./analytics-client\.js["'][^>]*></script>""",
        html,
        flags=re.IGNORECASE,
    ):
        raise SystemExit("The consent-aware first-party analytics client is not loaded.")
    client = ANALYTICS_CLIENT.read_text(encoding="utf-8")
    required_client_markers = (
        "TloombergAnalytics",
        "/api/v1/analytics/events",
        "page_view",
        "active_time",
        "scroll",
        "data-analytics-exclude",
    )
    if any(marker not in client for marker in required_client_markers):
        raise SystemExit("The analytics client is missing a required privacy-safe capture feature.")
    if re.search(r"localStorage|sessionStorage|document\.cookie", client):
        raise SystemExit("Behavioral events must not be persisted in browser storage.")
    if re.search(r"(?:innerText|textContent|outerHTML|value)\b", client):
        raise SystemExit("The analytics client must never inspect user-entered or rendered text.")
    if "privacy-analytics-panel" not in html or "/api/v1/analytics/consent" not in html:
        raise SystemExit("The explicit analytics consent UI is missing.")
    if "http://127.0.0.1:8000" in html:
        raise SystemExit("The Pages preview must not call a developer's localhost.")
    if re.search(
        r"(?:api[_-]?key|client[_-]?secret|merchant[_-]?id)\s*[:=]\s*['\"][^'\"]{8,}",
        html, re.I,
    ):
        raise SystemExit("A possible secret was found in the static HTML.")

    # The full app is JSX and is compiled in-browser by the pinned Babel
    # standalone script. Verify that the React/Babel entrypoint is present.
    if "babel.min.js" not in html or "react.production.min.js" not in html:
        raise SystemExit("React/Babel runtime dependencies are missing.")

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

    print("Pages preview valid: full UI structure, React/Babel runtime and public-data privacy.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
