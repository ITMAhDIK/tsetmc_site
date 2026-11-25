"""Pure wallet policy. All monetary values are integer Toman."""
from dataclasses import dataclass


@dataclass(frozen=True)
class WalletBalances:
    cash_toman: int
    gift_toman: int

    def __post_init__(self):
        if self.cash_toman < 0 or self.gift_toman < 0:
            raise ValueError("wallet bucket balances cannot be negative")

    @property
    def total_toman(self):
        return self.cash_toman + self.gift_toman

    @property
    def withdrawable_toman(self):
        return self.cash_toman


def validate_withdrawal(balances: WalletBalances, amount_toman: int) -> None:
    if not isinstance(amount_toman, int) or isinstance(amount_toman, bool):
        raise TypeError("withdrawal amount must be integer Toman")
    if amount_toman <= 0:
        raise ValueError("withdrawal amount must be positive")
    if amount_toman > balances.withdrawable_toman:
        raise ValueError("withdrawal exceeds user-funded cash balance")


def spend_buckets(balances: WalletBalances, amount_toman: int) -> WalletBalances:
    """Spend gift first, then cash. Caller persists debits atomically."""
    if not isinstance(amount_toman, int) or isinstance(amount_toman, bool):
        raise TypeError("spend amount must be integer Toman")
    if amount_toman <= 0:
        raise ValueError("spend amount must be positive")
    if amount_toman > balances.total_toman:
        raise ValueError("insufficient wallet balance")
    gift_spent = min(balances.gift_toman, amount_toman)
    cash_spent = amount_toman - gift_spent
    return WalletBalances(balances.cash_toman - cash_spent, balances.gift_toman - gift_spent)
