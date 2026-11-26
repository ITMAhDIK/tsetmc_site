"""Authenticated wallet and referral endpoints."""
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from saas_core.auth.api import current_user, get_conn
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .wallet_repository import (get_wallet_balances,list_wallet_ledger,
    request_withdrawal,cancel_withdrawal,review_withdrawal,settle_withdrawal)
from .referrals import ensure_referral_code,attach_referral,referral_summary

router=APIRouter(prefix="/wallet",tags=["wallet"])

class WithdrawalBody(BaseModel):
    amount_toman: int = Field(gt=0,le=10_000_000_000)
    bank_account_id: UUID

class ReferralAttachBody(BaseModel):
    code: str = Field(min_length=4,max_length=24)

@router.get("")
def wallet(user=Depends(current_user),conn=Depends(get_conn)):
    balances=get_wallet_balances(conn,user["id"])
    return {"cash_toman":balances.cash_toman,"gift_toman":balances.gift_toman,
            "total_toman":balances.total_toman,"reserved_cash_toman":balances.reserved_cash_toman,
            "withdrawable_toman":balances.withdrawable_toman}

@router.get("/ledger")
def ledger(limit:int=50,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return {"entries":list_wallet_ledger(conn,user["id"],limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))

@router.post("/withdrawals",status_code=201)
def withdrawal(body:WithdrawalBody,idempotency_key:str=Header(alias="Idempotency-Key",min_length=8,max_length=120),
               user=Depends(current_user),conn=Depends(get_conn)):
    try:
        row=request_withdrawal(conn,user_id=user["id"],amount_toman=body.amount_toman,
                               bank_account_id=body.bank_account_id,idempotency_key=idempotency_key)
        return {"withdrawal":row}
    except PermissionError as exc:
        raise HTTPException(status_code=403,detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))

@router.post("/withdrawals/{withdrawal_id}/cancel")
def withdrawal_cancel(withdrawal_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    row=cancel_withdrawal(conn,user_id=user["id"],withdrawal_id=withdrawal_id)
    if not row:
        raise HTTPException(status_code=404,detail="cancellable withdrawal not found")
    return {"withdrawal":row}

@router.get("/referrals")
def referrals(user=Depends(current_user),conn=Depends(get_conn)):
    return {"code":ensure_referral_code(conn,user["id"]),
            "summary":referral_summary(conn,user["id"])}

@router.post("/referrals/attach",status_code=201)
def attach(body:ReferralAttachBody,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return {"referral":attach_referral(conn,referred_user_id=user["id"],code=body.code)}
    except PermissionError as exc:
        raise HTTPException(status_code=403,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))


class WithdrawalReviewBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = Field(min_length=1,max_length=500)


class WithdrawalSettlementBody(BaseModel):
    payout_reference: str = Field(min_length=4,max_length=160)


def _require_withdrawal_reviewer(conn,user):
    try:
        require_permission(conn,user["id"],"withdrawals.review")
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="withdrawal review permission required") from exc


@router.get("/admin/withdrawals")
def admin_withdrawals(limit:int=100,user=Depends(current_user),conn=Depends(get_conn)):
    _require_withdrawal_reviewer(conn,user)
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422,detail="limit must be between 1 and 500")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,user_id,amount_toman,status,requested_at,reviewed_at,review_note
               FROM withdrawal_requests WHERE status IN ('requested','approved')
               ORDER BY requested_at LIMIT %s""",(limit,)
        )
        return {"withdrawals":cur.fetchall()}


@router.post("/admin/withdrawals/{withdrawal_id}/review")
def admin_review_withdrawal(withdrawal_id:UUID,body:WithdrawalReviewBody,
                            user=Depends(current_user),conn=Depends(get_conn)):
    _require_withdrawal_reviewer(conn,user)
    try:
        return {"withdrawal":review_withdrawal(
            conn,withdrawal_id=withdrawal_id,reviewer_id=user["id"],
            decision=body.decision,review_note=body.note,
        )}
    except LookupError as exc:
        raise HTTPException(status_code=404,detail="withdrawal request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.post("/admin/withdrawals/{withdrawal_id}/settle")
def admin_settle_withdrawal(withdrawal_id:UUID,body:WithdrawalSettlementBody,
                            user=Depends(current_user),conn=Depends(get_conn)):
    _require_withdrawal_reviewer(conn,user)
    try:
        return {"withdrawal":settle_withdrawal(
            conn,withdrawal_id=withdrawal_id,reviewer_id=user["id"],
            idempotency_key=f"withdrawal-paid:{withdrawal_id}",
            payout_reference=body.payout_reference,
        )}
    except LookupError as exc:
        raise HTTPException(status_code=404,detail="withdrawal request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
