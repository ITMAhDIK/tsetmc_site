import pytest

from saas_core.payments.domain import (
    PaymentRejected,
    VerifiedPayment,
    validate_order_amount,
    validate_verified_payment,
    PaymentNotConfigured,
)
from saas_core.payments.gateways import get_gateway
from saas_core.auth.api import app


def test_order_amount_must_be_positive_integer_toman():
    for value in (0, -1, 1.5, True, "1000"):
        with pytest.raises(ValueError):
            validate_order_amount(value)
    validate_order_amount(899000)


def test_verified_payment_must_match_expected_amount():
    verified = VerifiedPayment(
        provider_event_id="evt-1",
        provider_order_id="gateway-1",
        amount_toman=899000,
        success=True,
        reference="ref-1",
    )
    validate_verified_payment(expected_amount_toman=899000, verified=verified)


def test_amount_mismatch_is_rejected():
    verified = VerifiedPayment(
        provider_event_id="evt-2",
        provider_order_id="gateway-2",
        amount_toman=899001,
        success=True,
    )
    with pytest.raises(PaymentRejected, match="does not match"):
        validate_verified_payment(expected_amount_toman=899000, verified=verified)


def test_unsuccessful_provider_result_is_rejected():
    verified = VerifiedPayment(
        provider_event_id="evt-3",
        provider_order_id="gateway-3",
        amount_toman=899000,
        success=False,
    )
    with pytest.raises(PaymentRejected):
        validate_verified_payment(expected_amount_toman=899000, verified=verified)


@pytest.mark.parametrize("provider", ["snappay", "digipay"])
def test_real_gateway_is_explicitly_unconfigured_until_merchant_contract(provider):
    gateway = get_gateway(provider)
    with pytest.raises(PaymentNotConfigured):
        gateway.initiate(None)


def test_payment_routes_are_registered_on_saas_api():
    paths = app.openapi()["paths"]
    assert "/payments/orders" in paths
    assert "/payments/orders/{order_id}/checkout" in paths
    assert "/payments/orders/{order_id}" in paths
    assert "/payments/providers/{provider}/return" in paths
    assert "/payments/admin/orders/{order_id}/refunds" in paths
    assert "/payments/admin/reconciliation/run" in paths
    assert "/payments/admin/reconciliation" in paths


@pytest.mark.parametrize("provider", ["snappay", "digipay"])
def test_unconfigured_gateway_verification_and_refund_fail_closed(provider):
    gateway = get_gateway(provider)
    with pytest.raises(PaymentNotConfigured):
        gateway.verify(provider_order_id="gateway-1", callback_data={})
    with pytest.raises(PaymentNotConfigured):
        gateway.refund(provider_order_id="gateway-1", amount_toman=1000, idempotency_key="refund-123456")


def test_processed_provider_event_retry_is_idempotent():
    from contextlib import nullcontext
    from uuid import uuid4
    from saas_core.payments.settlement import settle_provider_callback

    order_id = uuid4()

    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, query, params=None):
            self.query = query
        def fetchone(self):
            return {"order_id": order_id, "processing_status": "processed"}

    class Connection:
        def transaction(self):
            return nullcontext()
        def cursor(self):
            return Cursor()

    verified = VerifiedPayment(
        provider_event_id="evt-retry",
        provider_order_id="gateway-1",
        amount_toman=899000,
        success=True,
    )
    result = settle_provider_callback(
        Connection(), order_id=order_id, attempt_id=uuid4(),
        provider="snappay", verified=verified, payload_sha256="a" * 64,
    )
    assert result == {"already_processed": True, "order_id": order_id}


def test_provider_event_cannot_be_replayed_against_another_order():
    from contextlib import nullcontext
    from uuid import uuid4
    from saas_core.payments.settlement import settle_provider_callback

    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, query, params=None):
            self.query = query
        def fetchone(self):
            return {"order_id": uuid4(), "processing_status": "processed"}

    class Connection:
        def transaction(self):
            return nullcontext()
        def cursor(self):
            return Cursor()

    verified = VerifiedPayment(
        provider_event_id="evt-replay",
        provider_order_id="gateway-1",
        amount_toman=899000,
        success=True,
    )
    with pytest.raises(PaymentRejected, match="another order"):
        settle_provider_callback(
            Connection(), order_id=uuid4(), attempt_id=uuid4(),
            provider="snappay", verified=verified, payload_sha256="b" * 64,
        )
