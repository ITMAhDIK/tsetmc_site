import pytest
from saas_core.domain.wallet import WalletBalances, spend_buckets, validate_withdrawal


def test_gift_is_spent_before_cash():
    result=spend_buckets(WalletBalances(500_000,200_000),250_000)
    assert result.gift_toman == 0
    assert result.cash_toman == 450_000


def test_gift_is_never_withdrawable():
    with pytest.raises(ValueError):
        validate_withdrawal(WalletBalances(100_000,900_000),100_001)


def test_reserved_withdrawals_reduce_available_cash():
    wallet=WalletBalances(500_000,300_000,200_000)
    assert wallet.withdrawable_toman == 300_000
    with pytest.raises(ValueError):
        validate_withdrawal(wallet,300_001)


@pytest.mark.parametrize("amount",[0,-1,True,1.5])
def test_withdrawal_amount_must_be_positive_integer(amount):
    with pytest.raises((TypeError,ValueError)):
        validate_withdrawal(WalletBalances(1000,1000),amount)
