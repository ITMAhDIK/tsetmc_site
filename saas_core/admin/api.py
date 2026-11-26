"""Authenticated administrative console API; each module checks its own permission."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from saas_core.auth.api import current_user,get_conn
from saas_core.auth.authorization import AuthorizationDenied
from .service import (
    console_overview,list_users,get_user_detail,set_user_status,list_roles,change_user_role,
    list_subscriptions,list_payment_orders,list_reconciliation,resolve_reconciliation,
    wallet_detail,adjust_wallet,list_withdrawals,list_referrals,list_audit_logs,list_support_queue,
)
router=APIRouter(prefix="/admin/console",tags=["admin-console"])

class UserStatusBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status: str=Field(pattern="^(active|suspended)$")
    reason: str=Field(min_length=1,max_length=500)
class WalletAdjustmentBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    bucket: str=Field(pattern="^(cash|gift)$")
    amount_toman: int
    reason: str=Field(min_length=1,max_length=500)
    idempotency_key: str=Field(min_length=8,max_length=120)
class ReconciliationBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    note: str=Field(min_length=1,max_length=500)
    dismissed: bool=False

def _call(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="required administrative permission is missing") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc

@router.get("/overview")
def overview(user=Depends(current_user),conn=Depends(get_conn)):
    return _call(console_overview,conn,actor_id=user["id"])
@router.get("/users")
def users(q:str|None=None,status:str|None=None,limit:int=50,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"users":_call(list_users,conn,actor_id=user["id"],query=q,status=status,limit=limit,offset=offset)}
@router.get("/users/{user_id}")
def user_detail(user_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    return _call(get_user_detail,conn,actor_id=user["id"],user_id=user_id)
@router.patch("/users/{user_id}/status")
def user_status(user_id:UUID,body:UserStatusBody,user=Depends(current_user),conn=Depends(get_conn)):
    return {"user":_call(set_user_status,conn,actor_id=user["id"],user_id=user_id,status=body.status,reason=body.reason)}
@router.get("/roles")
def roles(user=Depends(current_user),conn=Depends(get_conn)):
    return {"roles":_call(list_roles,conn,actor_id=user["id"])}
@router.put("/users/{user_id}/roles/{role_code}")
def grant_user_role(user_id:UUID,role_code:str,user=Depends(current_user),conn=Depends(get_conn)):
    return {"role":_call(change_user_role,conn,actor_id=user["id"],user_id=user_id,role_code=role_code,grant=True)}
@router.delete("/users/{user_id}/roles/{role_code}")
def revoke_user_role(user_id:UUID,role_code:str,user=Depends(current_user),conn=Depends(get_conn)):
    return {"role":_call(change_user_role,conn,actor_id=user["id"],user_id=user_id,role_code=role_code,grant=False)}
@router.get("/subscriptions")
def subscriptions(status:str|None=None,limit:int=100,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"subscriptions":_call(list_subscriptions,conn,actor_id=user["id"],status=status,limit=limit,offset=offset)}
@router.get("/payments")
def payments(status:str|None=None,limit:int=100,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"payments":_call(list_payment_orders,conn,actor_id=user["id"],status=status,limit=limit,offset=offset)}
@router.get("/payments/reconciliation")
def reconciliation(limit:int=100,user=Depends(current_user),conn=Depends(get_conn)):
    return {"cases":_call(list_reconciliation,conn,actor_id=user["id"],limit=limit)}
@router.post("/payments/reconciliation/{case_id}/resolve")
def reconciliation_resolve(case_id:UUID,body:ReconciliationBody,user=Depends(current_user),conn=Depends(get_conn)):
    return {"case":_call(resolve_reconciliation,conn,actor_id=user["id"],case_id=case_id,note=body.note,dismissed=body.dismissed)}
@router.get("/wallets/{user_id}")
def wallet(user_id:UUID,limit:int=50,user=Depends(current_user),conn=Depends(get_conn)):
    return _call(wallet_detail,conn,actor_id=user["id"],user_id=user_id,limit=limit)
@router.post("/wallets/{user_id}/adjust")
def wallet_adjust(user_id:UUID,body:WalletAdjustmentBody,user=Depends(current_user),conn=Depends(get_conn)):
    return {"entry":_call(adjust_wallet,conn,actor_id=user["id"],user_id=user_id,bucket=body.bucket,
        amount_toman=body.amount_toman,reason=body.reason,idempotency_key=body.idempotency_key)}
@router.get("/withdrawals")
def withdrawals(status:str|None=None,limit:int=100,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"withdrawals":_call(list_withdrawals,conn,actor_id=user["id"],status=status,limit=limit,offset=offset)}
@router.get("/referrals")
def referrals(status:str|None=None,limit:int=100,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"referrals":_call(list_referrals,conn,actor_id=user["id"],status=status,limit=limit,offset=offset)}
@router.get("/support")
def support(status:str|None=None,limit:int=100,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"tickets":_call(list_support_queue,conn,actor_id=user["id"],status=status,limit=limit,offset=offset)}
@router.get("/audit")
def audit(action:str|None=None,limit:int=100,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    return {"logs":_call(list_audit_logs,conn,actor_id=user["id"],action=action,limit=limit,offset=offset)}
