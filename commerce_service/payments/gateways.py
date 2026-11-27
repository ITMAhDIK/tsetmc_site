"""Provider adapters are intentionally disabled until merchant contracts exist.

Do not guess gateway URLs, credentials, signature rules or request payloads.
Implement each adapter from the merchant's current official integration pack.
"""
from .domain import PaymentNotConfigured

class _UnconfiguredGateway:
    def __init__(self, code: str):
        self.code = code

    def initiate(self, request):
        raise PaymentNotConfigured(f"{self.code} merchant adapter is not configured")

    def verify(self, *, provider_order_id: str, callback_data: dict):
        raise PaymentNotConfigured(f"{self.code} verification is not configured")

    def refund(self, *, provider_order_id: str, amount_toman: int, idempotency_key: str):
        raise PaymentNotConfigured(f"{self.code} refunds are not configured")

class SnapPayGateway(_UnconfiguredGateway):
    def __init__(self):
        super().__init__("snappay")

class DigiPayGateway(_UnconfiguredGateway):
    def __init__(self):
        super().__init__("digipay")

def get_gateway(provider: str):
    providers = {"snappay": SnapPayGateway, "digipay": DigiPayGateway}
    try:
        return providers[provider]()
    except KeyError as exc:
        raise ValueError("unsupported payment provider") from exc
