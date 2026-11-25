"""Provider-neutral payment domain rules.

Amounts are integer Toman. Browser redirects are never payment evidence.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

class PaymentError(Exception):
    """Base payment lifecycle error."""

class PaymentNotConfigured(PaymentError):
    """A live provider adapter/merchant configuration is not installed."""

class PaymentRejected(PaymentError):
    """The provider rejected or could not verify the payment."""

class OrderStatus(str, Enum):
    CREATED = "created"
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"

@dataclass(frozen=True)
class PaymentRequest:
    order_id: UUID
    amount_toman: int
    callback_url: str
    description: str

@dataclass(frozen=True)
class PaymentStart:
    provider_order_id: str
    redirect_url: str
    request_ref: str | None = None

@dataclass(frozen=True)
class VerifiedPayment:
    provider_event_id: str
    provider_order_id: str
    amount_toman: int
    success: bool
    reference: str | None = None

class PaymentGateway(Protocol):
    code: str

    def initiate(self, request: PaymentRequest) -> PaymentStart:
        """Create a provider payment and return its hosted checkout URL."""

    def verify(self, *, provider_order_id: str, callback_data: dict) -> VerifiedPayment:
        """Verify payment server-to-server; never trust browser callback fields."""

    def refund(self, *, provider_order_id: str, amount_toman: int, idempotency_key: str) -> str:
        """Request a provider refund and return its refund reference."""

def validate_order_amount(amount_toman: int) -> None:
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int) or amount_toman <= 0:
        raise ValueError("amount must be a positive integer in Toman")

def validate_verified_payment(*, expected_amount_toman: int, verified: VerifiedPayment) -> None:
    validate_order_amount(expected_amount_toman)
    validate_order_amount(verified.amount_toman)
    if not verified.success:
        raise PaymentRejected("provider reports an unsuccessful payment")
    if verified.amount_toman != expected_amount_toman:
        raise PaymentRejected("verified amount does not match the order")
    if not verified.provider_event_id or not verified.provider_order_id:
        raise PaymentRejected("provider verification response is incomplete")
