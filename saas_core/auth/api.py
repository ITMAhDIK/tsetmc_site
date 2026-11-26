"""FastAPI authentication API; deploy separately from GitHub Pages."""
import os
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
import psycopg
from psycopg.rows import dict_row
from .repository import (authenticate_session, consume_phone_otp, login_password,
    register_password_user, request_phone_otp, revoke_session, create_session)
from .service import AuthenticationError

app = FastAPI(title="Tloomberg SaaS API", version="0.3.0")
bearer = HTTPBearer(auto_error=False)
COOKIE_NAME = "tloomberg_session"
COOKIE_SECURE = os.getenv("SAAS_COOKIE_SECURE","true").lower() == "true"

def get_conn():
    dsn = os.getenv("SAAS_DATABASE_URL","")
    if not dsn:
        raise HTTPException(status_code=503, detail="SaaS database is not configured")
    try:
        with psycopg.connect(dsn,row_factory=dict_row) as conn:
            yield conn
    except psycopg.OperationalError as exc:
        raise HTTPException(status_code=503,detail="SaaS account service is unavailable") from exc

class RegisterBody(BaseModel):
    username: str = Field(min_length=3,max_length=64)
    password: str = Field(min_length=12,max_length=256)
    display_name: str | None = Field(default=None,max_length=160)
class PasswordLoginBody(BaseModel):
    username: str = Field(min_length=1,max_length=64)
    password: str = Field(min_length=1,max_length=256)
class OtpRequestBody(BaseModel):
    phone: str = Field(min_length=9,max_length=16)
    purpose: str = Field(default="login",pattern="^(signup|login)$")
class OtpVerifyBody(OtpRequestBody):
    otp: str = Field(min_length=6,max_length=6,pattern="^[0-9]{6}$")

def _set_session(response,token,expires):
    response.set_cookie(COOKIE_NAME,token,httponly=True,secure=COOKIE_SECURE,samesite="lax",expires=expires,path="/")
def _token(request,credentials):
    return (credentials.credentials if credentials else None) or request.cookies.get(COOKIE_NAME)

def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), conn=Depends(get_conn)):
    token = _token(request,credentials)
    user = authenticate_session(conn,token) if token else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="authentication required")
    return user

@app.get("/health")
def health():
    return {"status":"ok","service":"saas-api"}

class RegisterBody(BaseModel):
    username: str = Field(min_length=3,max_length=64)
    password: str = Field(min_length=12,max_length=256)
    display_name: str | None = Field(default=None,max_length=160)

@app.post("/auth/register")
def register(body:RegisterBody,response:Response,conn=Depends(get_conn)):
    try:
        user = register_password_user(conn,body.username,body.password,body.display_name)
        with conn.transaction():
            token,expires = create_session(conn,user["id"])
        _set_session(response,token,expires)
        return {"user":{"id":str(user["id"]),"public_id":user["public_id"],"username":user["username"],"display_name":user["display_name"]}}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409,detail="username is already in use")

@app.post("/auth/login")
def login(body:PasswordLoginBody,response:Response,conn=Depends(get_conn)):
    try:
        token,expires = login_password(conn,body.username,body.password)
        _set_session(response,token,expires)
        return {"authenticated":True,"expires_at":expires}
    except AuthenticationError:
        raise HTTPException(status_code=401,detail="invalid username or password")

@app.post("/auth/phone/request")
def phone_request(body:OtpRequestBody,conn=Depends(get_conn)):
    try:
        request_phone_otp(conn,body.phone,body.purpose)
        return {"accepted":True,"delivery":"queued","expires_in_seconds":300}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))

@app.post("/auth/phone/verify")
def phone_verify(body:OtpVerifyBody,response:Response,conn=Depends(get_conn)):
    try:
        token,expires = consume_phone_otp(conn,body.phone,body.otp,body.purpose)
        _set_session(response,token,expires)
        return {"authenticated":True,"expires_at":expires}
    except (AuthenticationError,ValueError):
        raise HTTPException(status_code=401,detail="invalid or expired verification code")

@app.get("/auth/me")
def me(user=Depends(current_user)):
    return {"user":{"id":str(user["id"]),"public_id":user["public_id"],"username":user["username"],
        "display_name":user["display_name"],"account_type":user["account_type"]}}

@app.post("/auth/logout")
def logout(request:Request,response:Response,credentials:HTTPAuthorizationCredentials | None=Depends(bearer),
           conn=Depends(get_conn),user=Depends(current_user)):
    revoke_session(conn,_token(request,credentials))
    response.delete_cookie(COOKIE_NAME,path="/",httponly=True,secure=COOKIE_SECURE,samesite="lax")
    return {"authenticated":False}

# Imported after auth dependencies are defined to avoid circular initialization.
from saas_core.account.api import router as account_router
app.include_router(account_router)

# Payment endpoints share the same isolated SaaS connection and session policy.
from saas_core.payments.api import router as payments_router
app.include_router(payments_router)


# Wallet routes share the authenticated SaaS session and isolated DB connection.
from saas_core.wallet.api import router as wallet_router
app.include_router(wallet_router)


# Support routes share authenticated sessions and isolated SaaS persistence.
from saas_core.support.api import router as support_router
app.include_router(support_router)
