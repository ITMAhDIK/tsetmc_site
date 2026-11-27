"""Identity session introspection and verified contact lookup."""
from __future__ import annotations
import json,os
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from uuid import UUID
from fastapi import Depends,HTTPException,Request as FastAPIRequest
from fastapi.security import HTTPAuthorizationCredentials,HTTPBearer
bearer=HTTPBearer(auto_error=False); COOKIE_NAME="tloomberg_session"
def _call(path,body=None):
 base=os.getenv("IDENTITY_SERVICE_URL","").strip().rstrip("/"); token=os.getenv("NOTIFICATION_IDENTITY_SERVICE_TOKEN","").strip()
 if not base or len(token)<32: raise RuntimeError("Identity unavailable")
 req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={"Content-Type":"application/json","X-Notification-Service-Token":token},method="POST" if body is not None else "GET")
 try:
  with urlopen(req,timeout=3) as response: raw=response.read(65537)
 except HTTPError as e:
  if e.code in {400,401}: raise PermissionError() from None
  raise RuntimeError("Identity unavailable") from None
 except (URLError,TimeoutError,OSError): raise RuntimeError("Identity unavailable") from None
 if len(raw)>65536: raise RuntimeError("Identity response too large")
 try: return json.loads(raw)
 except ValueError: raise RuntimeError("Invalid Identity response") from None
def current_user(request:FastAPIRequest,credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
 token=credentials.credentials if credentials else request.cookies.get(COOKIE_NAME)
 if not token: raise HTTPException(401,detail="authentication required")
 try:
  p=_call("/internal/introspect",{"token":token})["principal"]; u=p["user"]
  if u.get("status")!="active" or not isinstance(p.get("permissions"),list): raise ValueError()
  return {"id":UUID(str(u["id"])),"permissions":p["permissions"],"roles":p.get("roles",[])}
 except PermissionError: raise HTTPException(401,detail="invalid session") from None
 except (RuntimeError,KeyError,ValueError,TypeError): raise HTTPException(503,detail="Identity service unavailable") from None
def verified_contact(user_id:UUID):
 try:
  data=_call(f"/internal/notification-contact/{user_id}")
  return data.get("contact",{})
 except PermissionError: return {}
