"""Session introspection against Identity; CRM never reads Identity tables."""
from __future__ import annotations
import json,os
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from uuid import UUID
from fastapi import Depends,HTTPException,Request as FastAPIRequest
from fastapi.security import HTTPAuthorizationCredentials,HTTPBearer
COOKIE_NAME="tloomberg_session"
bearer=HTTPBearer(auto_error=False)
def _introspect(token):
    base=os.getenv("IDENTITY_SERVICE_URL","").strip().rstrip("/"); secret=os.getenv("IDENTITY_SERVICE_TOKEN","").strip()
    if not base or len(secret)<32: raise RuntimeError("Identity unavailable")
    req=Request(base+"/internal/introspect",data=json.dumps({"token":token}).encode(),headers={"Content-Type":"application/json","X-Identity-Service-Token":secret},method="POST")
    try:
        with urlopen(req,timeout=3) as response: raw=response.read(65537)
    except HTTPError as exc:
        if exc.code>=500 or exc.code in {403,404,408,429}: raise RuntimeError("Identity unavailable") from None
        raise PermissionError("Invalid session") from None
    except (URLError,TimeoutError,OSError): raise RuntimeError("Identity unavailable") from None
    if len(raw)>65536: raise RuntimeError("Identity response too large")
    try:
        principal=json.loads(raw)["principal"]; user=principal["user"]
        if user.get("status")!="active": raise ValueError()
        roles=principal["roles"]; permissions=principal["permissions"]
        if not isinstance(roles,list) or not isinstance(permissions,list): raise ValueError()
        return {"id":UUID(str(user["id"])),"roles":roles,"permissions":permissions,"mfa_enabled":principal["mfa_enabled"],"mfa_verified_at":principal.get("mfa_verified_at")}
    except (KeyError,TypeError,ValueError): raise RuntimeError("Invalid Identity response") from None
def current_user(request:FastAPIRequest,credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
    token=credentials.credentials if credentials else request.cookies.get(COOKIE_NAME)
    if not token: raise HTTPException(401,detail="authentication required")
    try: return _introspect(token)
    except PermissionError: raise HTTPException(401,detail="invalid session") from None
    except RuntimeError: raise HTTPException(503,detail="Identity service unavailable") from None
