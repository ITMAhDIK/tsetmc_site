"""Authenticated administrative console API; each module checks its own permission."""
from uuid import UUID
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from saas_core.auth.api import current_user,get_conn,COOKIE_NAME
from saas_core.auth.authorization import AuthorizationDenied,require_permission
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
    amount_toman: int=Field(strict=True)
    reason: str=Field(min_length=1,max_length=500)
    idempotency_key: str=Field(min_length=8,max_length=120)
class ReconciliationBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    note: str=Field(min_length=1,max_length=500)
    dismissed: bool=False


def _require_same_origin_for_cookie(request: Request):
    # Bearer-token API clients are not browser-CSRF susceptible. Cookie-authenticated
    # browser mutations must originate from the configured public origin.
    if request.cookies.get(COOKIE_NAME):
        origin=request.headers.get("origin")
        expected=os.getenv("SAAS_PUBLIC_ORIGIN") or str(request.base_url).rstrip("/")
        if not origin or origin.rstrip("/") != expected.rstrip("/"):
            raise HTTPException(status_code=403,detail="same-origin request required")

_CONSOLE_HTML = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tloomberg | Admin Console</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d30;--line:#26364e;--muted:#9aabc3;--text:#eef4ff;--accent:#73a7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.7 system-ui,sans-serif}
header{padding:22px 28px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:16px}
h1{font-size:20px;margin:0}small,.muted{color:var(--muted)}main{max-width:1400px;margin:auto;padding:24px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.stat,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.stat strong{display:block;font-size:24px;direction:ltr;text-align:right}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
button{background:#1d3150;border:1px solid #34527c;color:var(--text);padding:9px 13px;border-radius:8px;cursor:pointer}
button:hover{border-color:var(--accent)}input,select{background:#0b1526;border:1px solid var(--line);color:var(--text);padding:9px;border-radius:8px;min-width:140px}
form{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}pre{white-space:pre-wrap;overflow:auto;max-height:560px;background:#08101d;padding:14px;border-radius:8px;direction:ltr;text-align:left}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}.panel h2{font-size:16px;margin:0 0 8px}
#message{min-height:24px;color:var(--muted)}@media(max-width:600px){header{padding:16px;align-items:flex-start;flex-direction:column}main{padding:14px}}
</style></head>
<body>
<header><div><h1>Tloomberg — کنسول مدیریت</h1><small>دسترسی‌ها در سمت سرور کنترل می‌شوند؛ این صفحه جایگزین مجوزهای API نیست.</small></div><button id="refresh">به‌روزرسانی</button></header>
<main>
<div id="message">در حال دریافت اطلاعات…</div>
<section id="stats" class="stats"></section>
<nav>
<button data-path="/admin/console/users">کاربران</button>
<button data-path="/admin/console/roles">نقش‌ها</button>
<button data-path="/admin/console/subscriptions">اشتراک‌ها</button>
<button data-path="/admin/console/payments">پرداخت‌ها</button>
<button data-path="/admin/console/payments/reconciliation">مغایرت پرداخت</button>
<button data-path="/admin/console/withdrawals">برداشت‌ها</button>
<button data-path="/admin/console/referrals">دعوت‌ها</button>
<button data-path="/admin/console/support">پشتیبانی</button>
<button data-path="/admin/console/audit">گزارش ممیزی</button>
<button data-path="/analytics/admin/overview">تحلیل محصول</button>
</nav>
<div class="grid">
<section class="panel"><h2>جست‌وجوی کاربر</h2><form id="search"><input id="query" placeholder="نام کاربری یا شناسه عمومی"><button>جست‌وجو</button></form></section>
<section class="panel"><h2>تغییر وضعیت حساب</h2><form id="status"><input id="statusUser" placeholder="شناسه کاربر" required><select id="statusValue"><option value="suspended">تعلیق</option><option value="active">فعال‌سازی</option></select><input id="statusReason" placeholder="دلیل" required><button>ثبت تغییر</button></form></section>
<section class="panel"><h2>مدیریت نقش</h2><form id="role"><input id="roleUser" placeholder="شناسه کاربر" required><select id="roleValue"><option>user</option><option>support</option><option>admin</option></select><select id="roleAction"><option value="grant">اعطای نقش</option><option value="revoke">لغو نقش</option></select><button>ثبت</button></form><small>نقش superadmin از این کنسول قابل اعطا یا لغو نیست.</small></section>
<section class="panel"><h2>اصلاح کیف پول</h2><form id="wallet"><input id="walletUser" placeholder="شناسه کاربر" required><select id="walletBucket"><option value="cash">نقدی</option><option value="gift">هدیه</option></select><input id="walletAmount" type="number" placeholder="مبلغ تومان؛ منفی برای بدهکار" required><input id="walletReason" placeholder="دلیل" required><input id="walletKey" placeholder="کلید یکتای عملیات" required><button>ثبت اصلاح</button></form></section>
<section class="panel"><h2>رسیدگی به مغایرت</h2><form id="reconcile"><input id="caseId" placeholder="شناسه پرونده" required><input id="caseNote" placeholder="یادداشت رسیدگی" required><select id="caseDismiss"><option value="false">حل‌شده</option><option value="true">رد پرونده</option></select><button>ثبت نتیجه</button></form></section>
</div>
<section class="panel" style="margin-top:14px"><h2 id="resultTitle">خلاصه کنسول</h2><pre id="result">{}</pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
let lastPath="/admin/console/overview";
async function api(path,options={}){const response=await fetch(path,{credentials:"same-origin",...options,headers:{"Content-Type":"application/json",...(options.headers||{})}});const data=await response.json().catch(()=>({detail:response.statusText}));if(!response.ok)throw new Error(data.detail||("HTTP "+response.status));return data}
function show(data,title){$("resultTitle").textContent=title||lastPath;$("result").textContent=JSON.stringify(data,null,2)}
function message(value){$("message").textContent=value}
async function load(path){lastPath=path;message("در حال بارگذاری…");try{const data=await api(path);show(data,path);message("اطلاعات دریافت شد")}catch(e){message(e.message)}}
async function refresh(){try{const d=await api("/admin/console/overview");const labels={total_users:"کل کاربران",active_users:"کاربران فعال",active_subscriptions:"اشتراک فعال",pending_payments:"پرداخت معلق",open_reconciliation_cases:"مغایرت باز",pending_withdrawals:"برداشت در انتظار",pending_referrals:"دعوت در انتظار",open_tickets:"تیکت باز",events_today:"رویداد امروز"};$("stats").replaceChildren(...Object.entries(labels).map(([k,v])=>{const box=document.createElement("div");box.className="stat";const label=document.createElement("small");label.textContent=v;const val=document.createElement("strong");val.textContent=d[k]??0;box.append(label,val);return box}));show(d,"خلاصه کنسول");message("به‌روز شد")}catch(e){message(e.message)}}
document.querySelectorAll("button[data-path]").forEach(b=>b.addEventListener("click",()=>{let path=b.dataset.path;if(path==="/analytics/admin/overview"){const day=new Date().toISOString().slice(0,10);path+="?start="+day+"&end="+day}load(path)}));
$("refresh").addEventListener("click",refresh);
$("search").addEventListener("submit",e=>{e.preventDefault();load("/admin/console/users?q="+encodeURIComponent($("query").value))});
async function submitForm(id,path,method,body){$(id).addEventListener("submit",async e=>{e.preventDefault();try{const verb=typeof method==="function"?method():method;const d=await api(path(),{method:verb,body:JSON.stringify(body())});show(d,"نتیجه عملیات");message("عملیات انجام شد");await refresh()}catch(err){message(err.message)}})}
submitForm("status",()=>"/admin/console/users/"+encodeURIComponent($("statusUser").value)+"/status","PATCH",()=>({status:$("statusValue").value,reason:$("statusReason").value}));
submitForm("role",()=>"/admin/console/users/"+encodeURIComponent($("roleUser").value)+"/roles/"+encodeURIComponent($("roleValue").value),()=>$("roleAction").value==="grant"?"PUT":"DELETE",()=>({}));
submitForm("wallet",()=>"/admin/console/wallets/"+encodeURIComponent($("walletUser").value)+"/adjust","POST",()=>({bucket:$("walletBucket").value,amount_toman:Number($("walletAmount").value),reason:$("walletReason").value,idempotency_key:$("walletKey").value}));
submitForm("reconcile",()=>"/admin/console/payments/reconciliation/"+encodeURIComponent($("caseId").value)+"/resolve","POST",()=>({note:$("caseNote").value,dismissed:$("caseDismiss").value==="true"}));
refresh();
</script></body></html>"""

def _call(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="required administrative permission is missing") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc

@router.get("/ui",response_class=HTMLResponse)
def console_ui(user=Depends(current_user),conn=Depends(get_conn)):
    _call(console_overview,conn,actor_id=user["id"])
    return HTMLResponse(_CONSOLE_HTML)

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
def user_status(user_id:UUID,body:UserStatusBody,request:Request,user=Depends(current_user),conn=Depends(get_conn)):
    _require_same_origin_for_cookie(request)
    return {"user":_call(set_user_status,conn,actor_id=user["id"],user_id=user_id,status=body.status,reason=body.reason)}
@router.get("/roles")
def roles(user=Depends(current_user),conn=Depends(get_conn)):
    return {"roles":_call(list_roles,conn,actor_id=user["id"])}
@router.put("/users/{user_id}/roles/{role_code}")
def grant_user_role(user_id:UUID,role_code:str,request:Request,user=Depends(current_user),conn=Depends(get_conn)):
    _require_same_origin_for_cookie(request)
    return {"role":_call(change_user_role,conn,actor_id=user["id"],user_id=user_id,role_code=role_code,grant=True)}
@router.delete("/users/{user_id}/roles/{role_code}")
def revoke_user_role(user_id:UUID,role_code:str,request:Request,user=Depends(current_user),conn=Depends(get_conn)):
    _require_same_origin_for_cookie(request)
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
def reconciliation_resolve(case_id:UUID,body:ReconciliationBody,request:Request,user=Depends(current_user),conn=Depends(get_conn)):
    _require_same_origin_for_cookie(request)
    return {"case":_call(resolve_reconciliation,conn,actor_id=user["id"],case_id=case_id,note=body.note,dismissed=body.dismissed)}
@router.get("/wallets/{user_id}")
def wallet(user_id:UUID,limit:int=50,user=Depends(current_user),conn=Depends(get_conn)):
    return _call(wallet_detail,conn,actor_id=user["id"],user_id=user_id,limit=limit)
@router.post("/wallets/{user_id}/adjust")
def wallet_adjust(user_id:UUID,body:WalletAdjustmentBody,request:Request,user=Depends(current_user),conn=Depends(get_conn)):
    _require_same_origin_for_cookie(request)
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
