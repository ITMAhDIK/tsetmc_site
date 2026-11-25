import pytest
from saas_core.domain.wallet import WalletBalances, spend_buckets, validate_withdrawal


def test_referral_gifts_do_not_increase_withdrawable_amount():
    wallet = WalletBalances(400_000, 800_000)
    assert wallet.total_toman == 1_200_000
    assert wallet.withdrawable_toman == 400_000
    validate_withdrawal(wallet, 400_000)


def test_withdrawal_cannot_include_referral_gifts():
    with pytest.raises(ValueError, match="exceeds user-funded"):
        validate_withdrawal(WalletBalances(400_000, 800_000), 500_000)


@pytest.mark.parametrize("amount", [0, -1])
def test_withdrawal_must_be_positive(amount):
    with pytest.raises(ValueError):
        validate_withdrawal(WalletBalances(100, 100), amount)


def test_pending_withdrawals_reduce_available_cash():
    wallet = WalletBalances(cash_toman=400_000, gift_toman=800_000, reserved_cash_toman=150_000)
    assert wallet.withdrawable_toman == 250_000
    with pytest.raises(ValueError, match="exceeds user-funded"):
        validate_withdrawal(wallet, 250_001)


def test_reserved_cash_cannot_exceed_cash_balance():
    with pytest.raises(ValueError, match="reserved cash"):
        WalletBalances(cash_toman=100, gift_toman=900, reserved_cash_toman=101)


def test_gift_is_spent_before_cash():
    assert spend_buckets(WalletBalances(400_000, 800_000), 500_000) == WalletBalances(400_000, 300_000)


def test_cash_is_spent_after_gift_is_exhausted():
    assert spend_buckets(WalletBalances(400_000, 100_000), 250_000) == WalletBalances(250_000, 0)


def test_cannot_spend_more_than_total_balance():
    with pytest.raises(ValueError, match="insufficient"):
        spend_buckets(WalletBalances(10, 20), 31)
