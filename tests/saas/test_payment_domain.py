import pytest

from saas_core.payments.domain import (
    PaymentRejected,
    VerifiedPayment,
    validate_order_amount,
    validate_verified_payment,
)
from saas_core.payments.gateways import get_gateway
from saas_core.payments.domain import PaymentNotConfigured

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
