"""Authenticated account/dashboard endpoints for the SaaS API."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .service import (
    feature_access, get_preferences, get_profile, list_sessions,
    revoke_one_session, revoke_other_sessions, subscription_history,
    update_preferences, update_profile,
)
from ..auth.api import current_user, get_conn
from ..auth.identity_client import identity_enabled, user_action, IdentityUnauthorized, IdentityUnavailable, IdentityRequestError
from fastapi import Request

router = APIRouter(prefix="/account", tags=["account"])

class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)

class PreferencesUpdate(BaseModel):
    theme: str | None = None
    calendar: str | None = None
    currency: str | None = None
    locale: str | None = None

@router.get("/profile")
def profile(user=Depends(current_user), conn=Depends(get_conn)):
    row = get_profile(conn, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"profile": row}

@router.patch("/profile")
def patch_profile(body: ProfileUpdate, user=Depends(current_user), conn=Depends(get_conn)):
    if "display_name" not in body.model_fields_set:
        raise HTTPException(status_code=422, detail="display_name must be provided")
    if identity_enabled():
        try:
            result = user_action("/internal/users/me/profile", user_token=user.get("_identity_token", ""),
                                 payload={"display_name": body.display_name}, method="PATCH")
            from ..auth.identity_client import sync_target_projection
            sync_target_projection(conn, result)
            row = result.get("principal", {}).get("user")
        except IdentityUnauthorized:
            raise HTTPException(status_code=401, detail="Identity rejected the session") from None
        except IdentityRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail="Identity rejected the profile update") from None
        except IdentityUnavailable:
            raise HTTPException(status_code=503, detail="Identity service is temporarily unavailable") from None
    else:
        row = update_profile(conn, user["id"], display_name=body.display_name)
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"profile": row}

@router.get("/preferences")
def preferences(user=Depends(current_user), conn=Depends(get_conn)):
    row = get_preferences(conn, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="preferences not found")
    return {"preferences": row}

@router.patch("/preferences")
def patch_preferences(body: PreferencesUpdate, user=Depends(current_user), conn=Depends(get_conn)):
    changes = body.model_dump(exclude_unset=True)
    try:
        return {"preferences": update_preferences(conn, user["id"], changes)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

@router.get("/sessions")
def sessions(user=Depends(current_user), conn=Depends(get_conn)):
    if identity_enabled():
        try:
            return user_action("/internal/sessions", user_token=user.get("_identity_token", ""), method="GET")
        except IdentityUnauthorized:
            raise HTTPException(status_code=401, detail="Identity rejected the session") from None
        except IdentityUnavailable:
            raise HTTPException(status_code=503, detail="Identity service is temporarily unavailable") from None
    return {"sessions": list_sessions(conn, user["id"], user["session_id"])}

@router.delete("/sessions/{session_id}")
def delete_session(session_id: UUID, user=Depends(current_user), conn=Depends(get_conn)):
    if identity_enabled():
        try:
            return user_action(f"/internal/sessions/{session_id}", user_token=user.get("_identity_token", ""), method="DELETE")
        except IdentityUnauthorized:
            raise HTTPException(status_code=401, detail="Identity rejected the session") from None
        except IdentityRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail="Identity rejected the session revocation") from None
        except IdentityUnavailable:
            raise HTTPException(status_code=503, detail="Identity service is temporarily unavailable") from None
    try:
        revoked = revoke_one_session(conn, user["id"], session_id, user["session_id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not revoked:
        raise HTTPException(status_code=404, detail="active session not found")
    return {"revoked": True}

@router.post("/sessions/revoke-others")
def delete_other_sessions(user=Depends(current_user), conn=Depends(get_conn)):
    if identity_enabled():
        try:
            return user_action("/internal/sessions/revoke-others", user_token=user.get("_identity_token", ""))
        except IdentityUnauthorized:
            raise HTTPException(status_code=401, detail="Identity rejected the session") from None
        except IdentityUnavailable:
            raise HTTPException(status_code=503, detail="Identity service is temporarily unavailable") from None
    count = revoke_other_sessions(conn, user["id"], user["session_id"])
    return {"revoked_count": count}

@router.get("/subscriptions")
def subscriptions(user=Depends(current_user), conn=Depends(get_conn)):
    return {"subscriptions": subscription_history(conn, user["id"])}

@router.get("/features")
def features(user=Depends(current_user), conn=Depends(get_conn)):
    return {"features": feature_access(conn, user["id"], principal=user)}
