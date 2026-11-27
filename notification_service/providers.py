"""Pluggable outbound delivery providers. Never log message bodies or destinations."""
from __future__ import annotations
import os,smtplib,json
from email.message import EmailMessage
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
class DeliveryError(Exception):
 def __init__(self,code,retryable=True): self.code=code; self.retryable=retryable; super().__init__(code)
def send_email(destination,subject,text_body,html_body=None):
 host=os.getenv("NOTIFICATION_SMTP_HOST","").strip(); user=os.getenv("NOTIFICATION_SMTP_USER",""); password=os.getenv("NOTIFICATION_SMTP_PASSWORD","")
 sender=os.getenv("NOTIFICATION_EMAIL_FROM","").strip()
 if not host or not sender: raise DeliveryError("email_provider_not_configured")
 msg=EmailMessage(); msg["From"]=sender; msg["To"]=destination; msg["Subject"]=subject; msg.set_content(text_body)
 if html_body: msg.add_alternative(html_body,subtype="html")
 try:
  with smtplib.SMTP(host,int(os.getenv("NOTIFICATION_SMTP_PORT","587")),timeout=10) as smtp:
   smtp.starttls()
   if user: smtp.login(user,password)
   refused=smtp.send_message(msg)
   if refused: raise DeliveryError("email_recipient_rejected",False)
 except (OSError,smtplib.SMTPException) as e: raise DeliveryError("email_transport_error") from None
 return None
def send_sms(destination,text_body):
 url=os.getenv("NOTIFICATION_SMS_WEBHOOK_URL","").strip(); token=os.getenv("NOTIFICATION_SMS_WEBHOOK_TOKEN","").strip()
 if not url or not token: raise DeliveryError("sms_provider_not_configured")
 if not url.startswith("https://"): raise DeliveryError("sms_provider_requires_https",False)
 req=Request(url,data=json.dumps({"to":destination,"text":text_body}).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token},method="POST")
 try:
  with urlopen(req,timeout=10) as response:
   if response.status>=300: raise DeliveryError("sms_provider_rejected",response.status>=500)
   raw=response.read(8192)
   try: return json.loads(raw).get("message_id")
   except ValueError: return None
 except HTTPError as e: raise DeliveryError("sms_provider_http_error",e.code>=500 or e.code==429) from None
 except (URLError,TimeoutError,OSError): raise DeliveryError("sms_provider_transport_error") from None
