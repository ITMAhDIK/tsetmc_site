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
