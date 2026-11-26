"""Encrypted SMS outbox dispatch for phone OTP notifications.

The worker never logs phone numbers, OTP values, API keys, message bodies or
provider response bodies. The browser and API process only enqueue encrypted
notification payloads; this module is the only place that sends them.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken


class SmsDeliveryError(RuntimeError):
    """A provider rejected a message or delivery could not be attempted."""


class SmsSender(Protocol):
    def send(self, receptor: str, message: str) -> None: ...


def format_otp_message(otp: str, purpose: str) -> str:
    if not isinstance(otp, str) or not re.fullmatch(r"[0-9]{6}", otp):
        raise ValueError("OTP must contain exactly six digits")
    if purpose not in {"login", "signup"}:
        raise ValueError("unsupported OTP message purpose")
    return f"کد تأیید ورود تلومبرگ: {otp}\nاین کد تا ۵ دقیقه معتبر است. آن را در اختیار کسی قرار ندهید."


class KavenegarSmsSender:
    """Minimal HTTPS client for Kavenegar's form-encoded SMS send endpoint."""

    def __init__(self, *, api_key: str, sender: str, timeout_seconds: float = 10.0):
        if not api_key or not re.fullmatch(r"[A-Za-z0-9_-]{16,160}", api_key):
            raise ValueError("SMS_API_KEY is missing or malformed")
        if not sender or len(sender) > 32:
            raise ValueError("SMS_SENDER is missing or malformed")
        if not 2 <= timeout_seconds <= 30:
            raise ValueError("SMS timeout is outside the allowed range")
        self._api_key = api_key
        self._sender = sender
        self._timeout = timeout_seconds

    def send(self, receptor: str, message: str) -> None:
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", receptor):
            raise SmsDeliveryError("invalid SMS destination")
        url = f"https://api.kavenegar.com/v1/{self._api_key}/sms/send.json"
        body = urlencode({
            "receptor": receptor,
            "sender": self._sender,
            "message": message,
        }).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise SmsDeliveryError("SMS provider returned an HTTP error")
                data = json.loads(response.read(64 * 1024).decode("utf-8"))
        except HTTPError as exc:
            raise SmsDeliveryError("SMS provider rejected the request") from None
        except (URLError, TimeoutError, OSError):
            raise SmsDeliveryError("SMS provider is temporarily unreachable") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SmsDeliveryError("SMS provider returned an invalid response") from None

        provider_status = data.get("return", {}).get("status") if isinstance(data, dict) else None
        if provider_status != 200:
            # Do not include provider text: it can contain phone numbers or other PII.
            raise SmsDeliveryError("SMS provider did not accept the message")


def _decrypt_outbox_message(cipher: Fernet, row: dict) -> tuple[str, str, str]:
    try:
        receptor = cipher.decrypt(row["destination_token"].encode("ascii")).decode("utf-8")
        payload = row["payload"]
        encrypted_payload = payload["encrypted_payload"]
        decoded = cipher.decrypt(encrypted_payload.encode("ascii")).decode("utf-8")
        body = json.loads(decoded)
    except (InvalidToken, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise SmsDeliveryError("encrypted outbox payload is invalid") from None

    otp = body.get("otp")
    purpose = body.get("purpose")
    message = format_otp_message(otp, purpose)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", receptor):
        raise SmsDeliveryError("encrypted outbox destination is invalid")
    return receptor, otp, message


def dispatch_sms_batch(
    conn,
    *,
    sender: SmsSender,
    encryption_key: str,
    batch_size: int = 10,
    max_attempts: int = 5,
) -> dict[str, int]:
    """Claim and dispatch pending OTP rows with SKIP LOCKED and bounded retry.

    The DB lease is committed before network I/O. A crashed process releases
    work automatically after two minutes; a retry may result in a duplicate
    SMS if the provider accepted a message immediately before the crash.
    """
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")
    if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")
    try:
        cipher = Fernet(encryption_key.encode("ascii"))
    except Exception as exc:
        raise ValueError("SAAS_OUTBOX_FERNET_KEY must be a valid Fernet key") from exc

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """WITH due AS (
                     SELECT id
                     FROM notification_outbox
                     WHERE channel='sms' AND template_code='phone_otp'
                       AND status='pending' AND next_attempt_at<=now()
                       AND (locked_at IS NULL OR locked_at < now()-interval '2 minutes')
                     ORDER BY next_attempt_at,created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT %s
                   )
                   UPDATE notification_outbox AS o
                   SET locked_at=now(),attempts=o.attempts+1
                   FROM due
                   WHERE o.id=due.id
                   RETURNING o.id,o.destination_token,o.payload,o.attempts""",
                (batch_size,),
            )
            claimed = cur.fetchall()

    delivered = 0
    deferred = 0
    failed = 0
    for row in claimed:
        try:
            receptor, _otp, message = _decrypt_outbox_message(cipher, row)
            sender.send(receptor, message)
        except Exception:
            attempts = int(row["attempts"])
            is_failed = attempts >= max_attempts
            retry_seconds = min(900, 30 * (2 ** max(0, attempts - 1)))
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE notification_outbox
                           SET status=%s,locked_at=NULL,last_error=%s,
                               next_attempt_at=now()+(%s * interval '1 second')
                           WHERE id=%s AND status='pending'""",
                        (
                            "failed" if is_failed else "pending",
                            "sms_delivery_failed",
                            retry_seconds,
                            row["id"],
                        ),
                    )
            if is_failed:
                failed += 1
            else:
                deferred += 1
            continue

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE notification_outbox
                       SET status='sent',sent_at=now(),locked_at=NULL,last_error=NULL
                       WHERE id=%s AND status='pending'""",
                    (row["id"],),
                )
        delivered += 1

    return {
        "claimed": len(claimed),
        "delivered": delivered,
        "deferred": deferred,
        "failed": failed,
    }


def run_sms_worker() -> None:
    """Long-running production worker; configuration comes only from env."""
    import logging
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("tloomberg.saas.sms_worker")

    dsn = os.getenv("SAAS_DATABASE_URL", "").strip()
    expected_db = os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip()
    encryption_key = os.getenv("SAAS_OUTBOX_FERNET_KEY", "").strip()
    provider_name = os.getenv("SMS_PROVIDER", "").strip().lower()
    api_key = os.getenv("SMS_API_KEY", "").strip()
    sms_sender = os.getenv("SMS_SENDER", "").strip()
    try:
        poll_seconds = float(os.getenv("SAAS_NOTIFICATION_POLL_SECONDS", "2"))
    except ValueError as exc:
        raise SystemExit("SAAS_NOTIFICATION_POLL_SECONDS must be numeric") from exc

    if not dsn or not expected_db or not encryption_key:
        raise SystemExit("SaaS database, expected database name and outbox key are required")
    if provider_name != "kavenegar":
        raise SystemExit("SMS_PROVIDER must be configured as kavenegar")
    if not api_key or not sms_sender:
        raise SystemExit("SMS_API_KEY and SMS_SENDER are required")
    if not 1 <= poll_seconds <= 60:
        raise SystemExit("SAAS_NOTIFICATION_POLL_SECONDS must be between 1 and 60")
    try:
        Fernet(encryption_key.encode("ascii"))
        conninfo_to_dict(dsn)
    except Exception as exc:
        raise SystemExit("SMS worker database or encryption configuration is invalid") from exc

    sms = KavenegarSmsSender(api_key=api_key, sender=sms_sender)
    logger.info("SMS outbox worker started; provider=%s", provider_name)

    while True:
        try:
            with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
                database_name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
                if database_name != expected_db:
                    raise RuntimeError("connected database does not match configured SaaS database")
                conn.commit()
                result = dispatch_sms_batch(
                    conn,
                    sender=sms,
                    encryption_key=encryption_key,
                    batch_size=10,
                    max_attempts=5,
                )
            if result["claimed"]:
                logger.info(
                    "SMS batch complete: claimed=%d delivered=%d deferred=%d failed=%d",
                    result["claimed"], result["delivered"], result["deferred"], result["failed"],
                )
            else:
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            logger.info("SMS outbox worker stopping")
            return
        except Exception as exc:
            # Exception text from libraries/providers may include URLs or personal
            # data; log only the class, never str(exc) or a traceback.
            logger.error("SMS worker cycle failed (%s)", type(exc).__name__)
            time.sleep(min(10, poll_seconds * 2))
