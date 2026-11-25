#!/usr/bin/env python3
"""Run a bounded, read-only availability/load probe against an approved HTTPS staging API.

The harness refuses localhost/private IP targets, non-HTTPS URLs, URL paths/query
strings, missing host confirmation and missing non-production attestation. It only
sends GET requests to /health and /ready and never prints response bodies/tokens.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit


APPROVAL = "I_CONFIRM_NON_PRODUCTION_STAGING"
PATHS = ("/health", "/ready")
MAX_DURATION_SECONDS = 120
MAX_REQUESTS_PER_SECOND = 10
MAX_CONCURRENCY = 25
REQUEST_TIMEOUT_SECONDS = 5


class LoadTestConfigurationError(ValueError):
    """Unsafe or invalid load-test configuration."""


@dataclass(frozen=True)
class LoadConfig:
    base_url: str
    host: str
    duration_seconds: int
    requests_per_second: int
    concurrency: int


@dataclass(frozen=True)
class RequestResult:
    status_code: int
    duration_ms: float
    error_type: str | None = None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent a staging probe from following a redirect to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise LoadTestConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise LoadTestConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def build_config(environ: dict[str, str] | None = None) -> LoadConfig:
    env = os.environ if environ is None else environ
    approval = env.get("SAAS_LOAD_TEST_APPROVAL", "")
    if approval != APPROVAL:
        raise LoadTestConfigurationError(
            "Explicit non-production staging approval is required"
        )

    raw_url = env.get("SAAS_LOAD_TEST_BASE_URL", "").strip()
    if not raw_url:
        raise LoadTestConfigurationError("SAAS_LOAD_TEST_BASE_URL is required")

    parsed = urlsplit(raw_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LoadTestConfigurationError(
            "Target must be a bare HTTPS origin without credentials, path, query or fragment"
        )

    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise LoadTestConfigurationError("Private/reserved IP targets are not accepted")

    if host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise LoadTestConfigurationError("Local/internal/test-only hostnames are not accepted")
    if "." not in host:
        raise LoadTestConfigurationError("Target must be a public DNS hostname")

    confirmed_host = env.get("SAAS_LOAD_TEST_CONFIRM_HOST", "").strip().lower().rstrip(".")
    if confirmed_host != host:
        raise LoadTestConfigurationError(
            "SAAS_LOAD_TEST_CONFIRM_HOST must exactly match the target hostname"
        )

    netloc = host
    if parsed.port and parsed.port != 443:
        netloc = f"{host}:{parsed.port}"

    return LoadConfig(
        base_url=f"https://{netloc}",
        host=host,
        duration_seconds=_bounded_int(
            "SAAS_LOAD_TEST_DURATION_SECONDS", 30, 1, MAX_DURATION_SECONDS
        ),
        requests_per_second=_bounded_int(
            "SAAS_LOAD_TEST_REQUESTS_PER_SECOND", 2, 1, MAX_REQUESTS_PER_SECOND
        ),
        concurrency=_bounded_int(
            "SAAS_LOAD_TEST_CONCURRENCY", 4, 1, MAX_CONCURRENCY
        ),
    )


def _request_once(opener, url: str, scheduled_at: float) -> RequestResult:
    delay = scheduled_at - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "Tloomberg-Staging-Readiness-Probe/1.0",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            # Do not retain/print response bodies. Read only a small amount so
            # the HTTP connection can be reused/closed cleanly.
            response.read(1024)
            return RequestResult(
                status_code=int(response.status),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
    except urllib.error.HTTPError as exc:
        return RequestResult(
            status_code=int(exc.code),
            duration_ms=(time.perf_counter() - started) * 1000,
            error_type="http_error",
        )
    except Exception as exc:  # Network/TLS errors only; the exception text may contain URLs.
        return RequestResult(
            status_code=0,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
        )


def run_probe(config: LoadConfig) -> dict[str, object]:
    """Send only read-only health requests at the configured global rate."""
    request_count = config.duration_seconds * config.requests_per_second
    start_at = time.monotonic() + 0.5
    opener = urllib.request.build_opener(NoRedirectHandler())
    results: list[RequestResult] = []

    def scheduled_requests():
        for index in range(request_count):
            path = PATHS[index % len(PATHS)]
            scheduled_at = start_at + (index / config.requests_per_second)
            yield (opener, f"{config.base_url}{path}", scheduled_at)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.concurrency, thread_name_prefix="saas-staging-probe"
    ) as pool:
        results = list(pool.map(lambda args: _request_once(*args), scheduled_requests()))

    durations = sorted(result.duration_ms for result in results)

    def percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, int((len(values) - 1) * p)))
        return round(values[index], 2)

    status_counts: dict[str, int] = {}
    for result in results:
        key = str(result.status_code)
        status_counts[key] = status_counts.get(key, 0) + 1

    failures = sum(
        result.status_code == 0 or result.status_code >= 500 for result in results
    )
    return {
        "target_host": config.host,
        "mode": "read_only_staging_probe",
        "request_count": len(results),
        "requests_per_second": config.requests_per_second,
        "duration_seconds": config.duration_seconds,
        "max_concurrency": config.concurrency,
        "status_counts": status_counts,
        "network_or_server_failures": failures,
        "latency_ms": {
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "max": round(max(durations), 2) if durations else 0.0,
        },
        "paths": list(PATHS),
    }


def main() -> int:
    try:
        config = build_config()
        result = run_probe(config)
    except LoadTestConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Probe interrupted by operator.", file=sys.stderr)
        return 130

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["network_or_server_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
