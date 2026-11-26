#!/usr/bin/env python3
"""Fail-closed validation for the sanitized static GitHub Pages bundle."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def fail(message: str) -> None:
    raise SystemExit(f"Pages validation failed: {message}")


def main() -> int:
    required = (
        DOCS / "index.html",
        DOCS / "analytics-config.js",
        DOCS / "analytics.js",
        DOCS / "analytics.css",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        fail("missing required public assets: " + ", ".join(missing))

    html = (DOCS / "index.html").read_text(encoding="utf-8")
    config = (DOCS / "analytics-config.js").read_text(encoding="utf-8")
    if 'apiBase: ""' not in config:
        fail("public Pages config must keep analytics/auth API disabled until an approved API host is configured")
    for asset in ("./analytics-config.js", "./analytics.js", "./analytics.css"):
        if asset not in html:
            fail(f"index.html does not load {asset}")
    if "SAAS_SUPERADMIN_PHONE_E164=" in html or "SMS_API_KEY=" in html:
        fail("server-only authentication secrets must never appear in the public HTML")

    secret_markers = (
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "SAAS_DATABASE_URL=postgresql://",
        "SAAS_MIGRATION_DATABASE_URL=postgresql://",
    )
    public_asset_suffixes = {".html", ".js", ".css", ".json", ".svg", ".xml"}
    for path in DOCS.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith(".env") or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            fail(f"private configuration or key file is inside docs/: {path.relative_to(ROOT)}")
        if path.suffix.lower() not in public_asset_suffixes:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for marker in secret_markers:
            if marker in content:
                fail(f"secret-like marker found in {path.relative_to(ROOT)}")

    node = shutil.which("node")
    if not node:
        fail("Node.js is required to validate the browser analytics SDK")
    commands = [
        [node, "--check", str(DOCS / "analytics-config.js")],
        [node, "--check", str(DOCS / "analytics.js")],
        [node, "--test", str(ROOT / "tests" / "pages" / "analytics.test.cjs")],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        if result.returncode:
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            fail("command failed: " + " ".join(command))
    print("Pages bundle validated; public API config remains disabled and no private credentials are included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
