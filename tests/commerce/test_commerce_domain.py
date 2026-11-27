import pytest

from commerce_service.payments.domain import (
    PaymentNotConfigured, PaymentRejected, VerifiedPayment,
    validate_order_amount, validate_verified_payment,
)
from commerce_service.payments.gateways import get_gateway
from commerce_service.domain.wallet import WalletBalances, spend_buckets, validate_withdrawal


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "1000"])
def test_commerce_order_amount_is_positive_integer_toman(value):
    with pytest.raises(ValueError):
        validate_order_amount(value)


def test_verified_payment_requires_exact_server_verified_amount():
    verified = VerifiedPayment("evt-1", "provider-order-1", 899000, True)
    validate_verified_payment(expected_amount_toman=899000, verified=verified)
    with pytest.raises(PaymentRejected):
        validate_verified_payment(
            expected_amount_toman=899001,
            verified=verified,
        )


def test_unconfigured_provider_fails_closed():
    with pytest.raises(PaymentNotConfigured):
        get_gateway("snappay").initiate(None)


def test_wallet_spends_gift_before_cash_and_keeps_buckets_separate():
    result = spend_buckets(WalletBalances(500000, 200000), 250000)
    assert result.gift_toman == 0
    assert result.cash_toman == 450000


def test_wallet_withdrawal_cannot_use_gift_credit_or_reserved_cash():
    balances = WalletBalances(500000, 900000, 200000)
    validate_withdrawal(balances, 300000)
    with pytest.raises(ValueError):
        validate_withdrawal(balances, 300001)



def test_wallet_purchase_cannot_spend_cash_reserved_for_withdrawal():
    balances = WalletBalances(cash_toman=500000, gift_toman=0, reserved_cash_toman=200000)
    remaining = spend_buckets(balances, 200000)
    assert remaining.cash_toman == 300000
    assert remaining.reserved_cash_toman == 200000
    with pytest.raises(ValueError, match="insufficient spendable wallet balance"):
        spend_buckets(balances, 300001)
