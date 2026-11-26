"""Superadmin-only operational console API."""
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from saas_core.auth.api import current_user, get_conn, require_admin_mfa
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .service import (
    add_payment_reconciliation_note, change_user_role, change_user_status,
    cancel_subscription, dashboard_summary, get_admin_wallet, get_user_detail,
    list_audit_logs, list_payment_orders, list_plans, list_referrals, list_roles,
    list_subscriptions, list_users, list_withdrawals, review_withdrawal,
    update_plan, adjust_wallet, list_reconciliation_notes,
)

router=APIRouter(prefix="/admin",tags=["superadmin"],dependencies=[Depends(require_admin_mfa)])


def _require(conn,user_id,permission):
    try:
        require_permission(conn,user_id,"superadmin.console.access")
        require_permission(conn,user_id,permission)
    except AuthorizationDenied:
        raise HTTPException(status_code=403,detail="superadmin permission required")


class UserStatusBody(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")
    reason: str = Field(min_length=3,max_length=500)


class RoleBody(BaseModel):
    grant: bool


class PlanUpdateBody(BaseModel):
    title: str | None = Field(default=None,min_length=2,max_length=100)
    duration_days: int | None = Field(default=None,ge=1,le=3650,strict=True)
    price_toman: int | None = Field(default=None,ge=0,le=10**12,strict=True)
    active: bool | None = None


class SubscriptionCancelBody(BaseModel):
    reason: str = Field(min_length=3,max_length=500)


class ReconciliationNoteBody(BaseModel):
    note: str = Field(min_length=3,max_length=2000)


class WalletAdjustmentBody(BaseModel):
    bucket: str = Field(pattern="^(cash|gift)$")
    amount_toman: int = Field(strict=True,ge=-(2**63)+1,le=2**63-1)
    reason: str = Field(min_length=3,max_length=500)


class WithdrawalReviewBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = Field(min_length=3,max_length=500)


@router.get("/dashboard")
def dashboard(user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"users.read")
    return {"summary":dashboard_summary(conn)}


@router.get("/users")
def users(q:str|None=None,status:str|None=None,
          limit:int=Query(default=50,ge=1,le=100),
          offset:int=Query(default=0,ge=0,le=10_000),
          user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"users.read")
    try:
        return list_users(conn,query=q,status=status,limit=limit,offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.get("/users/{user_id}")
def user_detail(user_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"users.read")
    row=get_user_detail(conn,user_id=user_id)
    if not row:
        raise HTTPException(status_code=404,detail="user not found")
    return {"user":row}


@router.patch("/users/{user_id}/status")
def user_status(user_id:UUID,body:UserStatusBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"users.manage")
    try:
        return {"user":change_user_status(conn,actor_id=user["id"],user_id=user_id,
                                             status=body.status,reason=body.reason)}
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/roles")
def roles(user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"roles.manage")
    return {"roles":list_roles(conn)}


@router.put("/users/{user_id}/roles/{role_code}")
def user_role(user_id:UUID,role_code:str,body:RoleBody,
              user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"roles.manage")
    try:
        return change_user_role(conn,actor_id=user["id"],user_id=user_id,
                                role_code=role_code,grant=body.grant)
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/plans")
def plans(user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"subscriptions.manage")
    return {"plans":list_plans(conn)}


@router.patch("/plans/{plan_id}")
def plan_update(plan_id:UUID,body:PlanUpdateBody,
                user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"subscriptions.manage")
    try:
        row=update_plan(conn,actor_id=user["id"],plan_id=plan_id,
                        changes=body.model_dump(exclude_unset=True,exclude_none=True))
        return {"plan":row,"note":"Price changes affect new orders only."}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/subscriptions")
def subscriptions(status:str|None=None,limit:int=Query(default=50,ge=1,le=100),
                  offset:int=Query(default=0,ge=0,le=10_000),
                  user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"subscriptions.manage")
    try:
        return {"subscriptions":list_subscriptions(conn,status=status,limit=limit,offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.post("/subscriptions/{subscription_id}/cancel")
def subscription_cancel(subscription_id:UUID,body:SubscriptionCancelBody,
                         user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"subscriptions.manage")
    try:
        return {"subscription":cancel_subscription(conn,actor_id=user["id"],
                                                     subscription_id=subscription_id,reason=body.reason)}
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/payments/orders")
def payment_orders(status:str|None=None,limit:int=Query(default=50,ge=1,le=100),
                   offset:int=Query(default=0,ge=0,le=10_000),
                   user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"payments.read")
    try:
        return {"orders":list_payment_orders(conn,status=status,limit=limit,offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.post("/payments/orders/{order_id}/reconciliation-notes",status_code=201)
def payment_reconciliation(order_id:UUID,body:ReconciliationNoteBody,
                           idempotency_key:str=Header(alias="Idempotency-Key",min_length=8,max_length=120),
                           user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"payments.read")
    try:
        return {"note":add_payment_reconciliation_note(
            conn,actor_id=user["id"],order_id=order_id,note=body.note,
            idempotency_key=idempotency_key)}
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/payments/orders/{order_id}/reconciliation-notes")
def payment_reconciliation_history(order_id:UUID,limit:int=Query(default=50,ge=1,le=100),
                                   user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"payments.read")
    return {"notes":list_reconciliation_notes(conn,order_id=order_id,limit=limit)}


@router.get("/users/{user_id}/wallet")
def user_wallet(user_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"wallet.read")
    row=get_user_detail(conn,user_id=user_id)
    if not row:
        raise HTTPException(status_code=404,detail="user not found")
    return get_admin_wallet(conn,user_id=user_id)


@router.post("/users/{user_id}/wallet/adjustments",status_code=201)
def wallet_adjustment(user_id:UUID,body:WalletAdjustmentBody,
                      idempotency_key:str=Header(alias="Idempotency-Key",min_length=8,max_length=120),
                      user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"wallet.adjust")
    try:
        return {"entry":adjust_wallet(conn,actor_id=user["id"],user_id=user_id,
                                      bucket=body.bucket,amount_toman=body.amount_toman,
                                      reason=body.reason,idempotency_key=idempotency_key)}
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/withdrawals")
def withdrawals(status:str|None=None,limit:int=Query(default=50,ge=1,le=100),
                offset:int=Query(default=0,ge=0,le=10_000),
                user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"withdrawals.review")
    try:
        return {"withdrawals":list_withdrawals(conn,status=status,limit=limit,offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.post("/withdrawals/{withdrawal_id}/review")
def withdrawal_review(withdrawal_id:UUID,body:WithdrawalReviewBody,
                      user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"withdrawals.review")
    try:
        return {"withdrawal":review_withdrawal(conn,actor_id=user["id"],
                                                 withdrawal_id=withdrawal_id,
                                                 decision=body.decision,note=body.note)}
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/referrals")
def referrals(status:str|None=None,limit:int=Query(default=50,ge=1,le=100),
              offset:int=Query(default=0,ge=0,le=10_000),
              user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"users.read")
    try:
        return {"referrals":list_referrals(conn,status=status,limit=limit,offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.get("/audit-logs")
def audit_logs(actor_id:UUID|None=None,action:str|None=None,target_type:str|None=None,
               limit:int=Query(default=50,ge=1,le=100),
               offset:int=Query(default=0,ge=0,le=10_000),
               user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"audit.read")
    return {"logs":list_audit_logs(conn,actor_id=actor_id,action=action,
                                   target_type=target_type,limit=limit,offset=offset)}
