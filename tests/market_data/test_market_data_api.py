from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from market_data_service.api import app, get_repository


@pytest.fixture
def client(market_store):
    repository, _, _ = market_store
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_repository, None)


def test_health_and_readiness_are_distinct(client):
    assert client.get("/healthz").json() == {
        "status": "ok", "service": "market-data"
    }
    assert client.get("/readyz").json() == {
        "status": "ready", "service": "market-data"
    }


def test_instrument_catalogue_and_detail_contract(client):
    listing = client.get("/api/v1/market/instruments?q=فولاد&market=TSE")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    detail = client.get("/api/v1/market/instruments/فولاد")
    assert detail.status_code == 200
    assert detail.json()["instrument"]["cisin"] == "IR1234567890"


def test_legacy_search_contract_is_preserved(client):
    response = client.get("/api/v1/stocks/search?q=فولاد&limit=8")
    assert response.status_code == 200
    assert response.json() == {
        "results": [{
            "symbol": "فولاد",
            "company_name": "فولاد مبارکه اصفهان",
            "market": "TSE",
        }]
    }


def test_legacy_daily_prices_contract_is_preserved(client):
    response = client.get("/api/v1/stocks/فولاد/prices")
    assert response.status_code == 200
    assert len(response.json()["prices"]) == 2
    assert response.json()["prices"][0]["close"] == 2500


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/v1/market/instruments?market=FOREX", 422),
        ("/api/v1/stocks/unknown/prices", 404),
        ("/api/v1/market/instruments?limit=0", 422),
    ],
)
def test_bad_market_requests_have_stable_status_codes(client, path, status):
    assert client.get(path).status_code == status


def test_market_data_api_is_read_only(client):
    assert client.post("/api/v1/market/instruments", json={}).status_code == 405
    assert client.delete("/api/v1/market/instruments/فولاد").status_code == 405
