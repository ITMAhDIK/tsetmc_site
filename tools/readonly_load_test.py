"""Bounded GET-only load probe for an explicitly named staging environment."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ACK = "STAGING-ONLY"
ALLOWED_PATHS = {"/health/ready", "/api/v1/market/overview"}


class LoadTestError(ValueError):
    pass


def validate_target(url: str, *, environment: str, acknowledgement: str):
    if environment != "staging" or acknowledgement != ACK:
        raise LoadTestError("A staging environment and STAGING-ONLY acknowledgement are required.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname
        or "staging" not in parsed.hostname.lower()
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in ALLOWED_PATHS):
        raise LoadTestError("Target must be an HTTPS staging host and an allow-listed read-only path.")
    return url


def run_readonly_load(url, *, requests=100, concurrency=5, timeout=5.0,
                      bearer_token=None, opener=None):
    if isinstance(requests, bool) or not isinstance(requests, int) or not 1 <= requests <= 1000:
        raise LoadTestError("requests must be between 1 and 1000.")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 10:
        raise LoadTestError("concurrency must be between 1 and 10.")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.5 <= timeout <= 20:
        raise LoadTestError("timeout must be between 0.5 and 20 seconds.")
    opener = opener or urlopen

    def one(_):
        headers = {"Accept": "application/json"}
        if bearer_token:
            headers["Authorization"] = "Bearer " + bearer_token
        request = Request(url, headers=headers, method="GET")
        started = time.perf_counter()
        try:
            with opener(request, timeout=timeout) as response:
                body = response.read(65537)
                if len(body) > 65536:
                    raise LoadTestError("Read-only response exceeded 64 KiB.")
                status = int(response.status)
                okay = 200 <= status < 300
        except HTTPError as exc:
            status, okay = int(exc.code), False
        except (URLError, TimeoutError, OSError):
            status, okay = 0, False
        return okay, status, (time.perf_counter() - started) * 1000

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        outcomes = list(pool.map(one, range(requests)))
    latencies = sorted(item[2] for item in outcomes)
    success = sum(1 for item in outcomes if item[0])
    p95 = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95) - 1))
    statuses = {}
    for _ok, status, _latency in outcomes:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    return {
        "requests": requests, "concurrency": concurrency, "successes": success,
        "failures": requests - success, "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(latencies[p95], 2), "status_counts": statuses,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a bounded staging-only GET load probe.")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--ack-staging-traffic", default="")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
        target = validate_target(args.target_url, environment=args.environment,
                                 acknowledgement=args.ack_staging_traffic)
        result = run_readonly_load(
            target, requests=args.requests, concurrency=args.concurrency,
            timeout=args.timeout, bearer_token=os.environ.get("STAGING_READONLY_BEARER_TOKEN"),
        )
    except (LoadTestError, ValueError) as exc:
        print("staging-load-test: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
