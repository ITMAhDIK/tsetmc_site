"""Subscription purchase endpoints; gateway returns are verified server-side."""
import hashlib
import json
import os
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from saas_core.auth.api import current_user, get_conn
from .domain import PaymentNotConfigured, PaymentRejected, PaymentRequest
from .gateways import get_gateway
from .settlement import settle_provider_callback
from .repository import (
    attach_provider_checkout, create_payment_attempt, create_subscription_order,
    find_attempt_by_provider_order, get_order_for_user, mark_attempt_failed,
)

router = APIRouter(prefix="/payments", tags=["payments"])

class CreateOrderBody(BaseModel):
    plan_code: str = Field(min_length=3, max_length=32)

class CheckoutBody(BaseModel):
    provider: str = Field(pattern="^(snappay|digipay)$")

@router.post("/orders", status_code=201)
def create_order(
    body: CreateOrderBody,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=100),
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    try:
        order = create_subscription_order(
            conn, user_id=user["id"], plan_code=body.plan_code,
            idempotency_key=idempotency_key,
        )
        return {"order": order}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@router.post("/orders/{order_id}/checkout")
def checkout(
    order_id: UUID,
    body: CheckoutBody,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    order = get_order_for_user(conn, order_id=order_id, user_id=user["id"])
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    try:
        gateway = get_gateway(body.provider)
        callback_url = os.environ.get("SAAS_PAYMENT_CALLBACK_URL", "").strip()
        if not callback_url.startswith("https://"):
            raise PaymentNotConfigured("SAAS_PAYMENT_CALLBACK_URL must be configured with HTTPS")
        attempt = create_payment_attempt(conn, order_id=order_id, provider=body.provider)
        try:
            started = gateway.initiate(PaymentRequest(
                order_id=order_id,
                amount_toman=attempt["amount_toman"],
                callback_url=callback_url,
                description=f"Tloomberg subscription {order['plan_code']}",
            ))
            attach_provider_checkout(
                conn, attempt_id=attempt["id"],
                provider_order_id=started.provider_order_id,
                redirect_url=started.redirect_url,
                request_ref=started.request_ref,
            )
        except Exception as exc:
            mark_attempt_failed(
                conn, attempt_id=attempt["id"],
                failure_code=type(exc).__name__,
                failure_message="Gateway initiation failed",
            )
            raise
        return {
            "checkout_url": started.redirect_url,
            "provider": body.provider,
            "order_id": str(order_id),
            "expires_at": order["expires_at"],
        }
    except PaymentNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail="payment provider could not create checkout") from exc

@router.get("/orders/{order_id}")
def order_status(order_id: UUID, user=Depends(current_user), conn=Depends(get_conn)):
    order = get_order_for_user(conn, order_id=order_id, user_id=user["id"])
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order": order}

@router.post("/providers/{provider}/return")
async def provider_return(provider: str, request: Request, conn=Depends(get_conn)):
    if provider not in {"snappay", "digipay"}:
        raise HTTPException(status_code=404, detail="provider not found")
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("callback body must be an object")
        gateway = get_gateway(provider)
        provider_order_id = str(payload.get("provider_order_id", "")).strip()
        if not provider_order_id:
            raise ValueError("provider order reference is missing")
        attempt = find_attempt_by_provider_order(
            conn, provider=provider, provider_order_id=provider_order_id
        )
        if not attempt:
            raise LookupError("payment attempt not found")
        verified = gateway.verify(
            provider_order_id=provider_order_id,
            callback_data=payload,
        )
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = settle_provider_callback(
            conn, order_id=attempt["order_id"], attempt_id=attempt["attempt_id"],
            provider=provider, verified=verified, payload_sha256=digest,
        )
        return {"received": True, **result}
    except PaymentNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (PaymentRejected, ValueError) as exc:
        raise HTTPException(status_code=400, detail="payment verification failed") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
