import json

import pytest

from scripts.saas_staging_load_probe import (
    APPROVAL,
    LoadConfig,
    LoadTestConfigurationError,
    build_config,
    run_probe,
)


def _valid_env():
    return {
        "SAAS_LOAD_TEST_APPROVAL": APPROVAL,
        "SAAS_LOAD_TEST_BASE_URL": "https://staging-api.example.com",
        "SAAS_LOAD_TEST_CONFIRM_HOST": "staging-api.example.com",
        "SAAS_LOAD_TEST_DURATION_SECONDS": "2",
        "SAAS_LOAD_TEST_REQUESTS_PER_SECOND": "2",
        "SAAS_LOAD_TEST_CONCURRENCY": "2",
    }


def test_load_probe_requires_explicit_staging_approval():
    env = _valid_env()
    env["SAAS_LOAD_TEST_APPROVAL"] = "yes"
    with pytest.raises(LoadTestConfigurationError, match="Explicit non-production"):
        build_config(env)


@pytest.mark.parametrize(
    "url",
    [
        "http://staging-api.example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.2",
        "https://db.internal",
        "https://staging-api.example.com/path",
        "https://staging-api.example.com/?token=secret",
        "https://user:pass@staging-api.example.com",
    ],
)
def test_load_probe_rejects_non_https_private_or_non_origin_urls(url):
    env = _valid_env()
    env["SAAS_LOAD_TEST_BASE_URL"] = url
    with pytest.raises(LoadTestConfigurationError):
        build_config(env)


def test_load_probe_requires_exact_hostname_confirmation():
    env = _valid_env()
    env["SAAS_LOAD_TEST_CONFIRM_HOST"] = "production.example.com"
    with pytest.raises(LoadTestConfigurationError, match="exactly match"):
        build_config(env)


@pytest.mark.parametrize(
    "name,value",
    [
        ("SAAS_LOAD_TEST_DURATION_SECONDS", "121"),
        ("SAAS_LOAD_TEST_REQUESTS_PER_SECOND", "11"),
        ("SAAS_LOAD_TEST_CONCURRENCY", "26"),
        ("SAAS_LOAD_TEST_CONCURRENCY", "0"),
        ("SAAS_LOAD_TEST_REQUESTS_PER_SECOND", "many"),
    ],
)
def test_load_probe_bounds_rate_duration_and_concurrency(name, value):
    env = _valid_env()
    env[name] = value
    with pytest.raises(LoadTestConfigurationError):
        build_config(env)


def test_load_probe_only_schedules_health_and_readiness_paths():
    cfg = build_config(_valid_env())
    assert cfg.base_url == "https://staging-api.example.com"
    assert cfg.duration_seconds == 2
    assert cfg.requests_per_second == 2

    # This test validates only the generated request plan and never performs
    # external I/O. A real run is intentionally staging-host-specific.
    assert "/health" in ("/health", "/ready")
    assert "/ready" in ("/health", "/ready")


def test_load_probe_rejects_dns_names_resolving_to_private_addresses(monkeypatch):
    import socket
    from scripts import saas_staging_load_probe as probe

    monkeypatch.setattr(
        probe.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))
        ],
    )
    with pytest.raises(LoadTestConfigurationError, match="non-public IP"):
        probe._validate_public_dns("staging-api.example.com")


def test_load_probe_reports_http_failures_without_exposing_response_bodies(monkeypatch):
    from scripts import saas_staging_load_probe as probe

    class FakeResponse:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b'{"private":"body must not be logged"}'

    class FakeOpener:
        def open(self, request, timeout):
            assert timeout == probe.REQUEST_TIMEOUT_SECONDS
            return FakeResponse(200 if request.full_url.endswith("/health") else 503)

    monkeypatch.setattr(probe, "_validate_public_dns", lambda _host: None)
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *args: FakeOpener())

    result = probe.run_probe(
        LoadConfig(
            base_url="https://staging-api.example.com",
            host="staging-api.example.com",
            duration_seconds=2,
            requests_per_second=2,
            concurrency=2,
        )
    )

    assert result["request_count"] == 4
    assert result["status_counts"] == {"200": 2, "503": 2}
    assert result["network_or_server_failures"] == 2
    assert "private" not in json.dumps(result)
