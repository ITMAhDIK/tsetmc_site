"""PostgreSQL persistence helpers. Only the dedicated SaaS database is used."""
from __future__ import annotations
from uuid import UUID
from cryptography.fernet import Fernet
import os
from .service import (OTP_MAX_ATTEMPTS, OTP_TTL, SESSION_TTL, AuthenticationError,
    hash_otp, hash_password, hash_session_token, new_otp, new_session_token,
    normalize_phone, utc_now, verify_otp_hash, verify_password, _DUMMY_PASSWORD_HASH)

def _ensure_user_role(cur, user_id: UUID) -> None:
    cur.execute("""INSERT INTO user_roles(user_id, role_id)
        SELECT %s,id FROM roles WHERE code='user' ON CONFLICT DO NOTHING""", (user_id,))

def register_password_user(conn, username: str, password: str, display_name: str | None = None):
    username = username.strip().lower()
    if not 3 <= len(username) <= 64 or not username.replace("_","").replace(".","").isalnum():
        raise ValueError("username must be 3-64 characters and contain letters, numbers, . or _")
    password_hash = hash_password(password)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO users(public_id,username,display_name,password_hash)
                VALUES ('usr_' || encode(gen_random_bytes(12),'hex'),%s,%s,%s)
                RETURNING id,public_id,username,display_name,created_at""",
                (username, display_name, password_hash))
            user = cur.fetchone()
            _ensure_user_role(cur, user["id"])
            cur.execute("INSERT INTO auth_identities(user_id,provider,provider_subject) VALUES (%s,'password',%s)", (user["id"], username))
            cur.execute("INSERT INTO user_preferences(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user["id"],))
            cur.execute("INSERT INTO wallet_accounts(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user["id"],))
    return user

def create_session(conn, user_id: UUID):
    token = new_session_token()
    expires = utc_now() + SESSION_TTL
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sessions(user_id,token_hash,expires_at) VALUES (%s,%s,%s)",
                    (user_id, hash_session_token(token), expires))
    return token, expires

def login_password(conn, username: str, password: str):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id,password_hash,status FROM users WHERE lower(username)=lower(%s)", (username.strip(),))
            row = cur.fetchone()
            candidate_hash = (
                row["password_hash"] if row and row["password_hash"]
                else _DUMMY_PASSWORD_HASH
            )
            password_valid = verify_password(candidate_hash, password)
            if not row or row["status"] != "active" or not row["password_hash"] or not password_valid:
                raise AuthenticationError("invalid username or password")
            token, expires = create_session(conn, row["id"])
            cur.execute("UPDATE users SET last_login_at=now() WHERE id=%s", (row["id"],))
    return token, expires

def authenticate_session(conn, token: str):
    if not token or len(token) > 256:
        return None
    with conn.cursor() as cur:
        cur.execute("""SELECT u.id,u.public_id,u.username,u.display_name,u.account_type,u.status,s.id AS session_id
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() AND u.status='active'""",
            (hash_session_token(token),))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE sessions SET last_seen_at=now() WHERE id=%s", (row["session_id"],))
    return row

def revoke_session(conn, token: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sessions SET revoked_at=now() WHERE token_hash=%s AND revoked_at IS NULL",
                    (hash_session_token(token),))

def request_phone_otp(conn, phone: str, purpose: str = "login") -> None:
    phone = normalize_phone(phone)
    if purpose not in {"signup","login","purchase","link_identity","withdrawal"}:
        raise ValueError("unsupported OTP purpose")
    otp = new_otp()
    digest = hash_otp(phone, purpose, otp)
    expires = utc_now() + OTP_TTL
    with conn.transaction():
        with conn.cursor() as cur:
            # Serialize concurrent OTP requests for this normalized phone number.
            # A plain COUNT followed by INSERT would allow parallel requests to
            # race past the per-phone request ceiling.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (phone,),
            )
            cur.execute("""SELECT count(*) AS recent_count FROM phone_verifications
                WHERE phone_e164=%s AND created_at > now() - interval '15 minutes'""", (phone,))
            if cur.fetchone()["recent_count"] >= 3:
                raise ValueError("too many verification requests; try again later")
            cur.execute("""UPDATE phone_verifications SET consumed_at=now()
                WHERE phone_e164=%s AND purpose=%s AND consumed_at IS NULL""", (phone,purpose))
            cur.execute("""INSERT INTO phone_verifications(phone_e164,purpose,otp_hash,expires_at)
                VALUES (%s,%s,%s,%s)""", (phone,purpose,digest,expires))
            # Encrypt both destination and message body at rest. The SMS worker
            # decrypts them only when dispatching through the configured provider.
            key = os.environ.get("SAAS_OUTBOX_FERNET_KEY", "")
            try:
                cipher = Fernet(key.encode("ascii"))
            except Exception as exc:
                raise RuntimeError("SAAS_OUTBOX_FERNET_KEY must be a valid Fernet key") from exc
            encrypted_phone = cipher.encrypt(phone.encode("utf-8")).decode("ascii")
            encrypted_payload = cipher.encrypt(
                ('{"otp":"' + otp + '","purpose":"' + purpose + '"}').encode("utf-8")
            ).decode("ascii")
            cur.execute("""INSERT INTO notification_outbox(channel,template_code,destination_token,payload,idempotency_key)
                VALUES ('sms','phone_otp',%s,jsonb_build_object('encrypted_payload',%s),%s)""",
                (encrypted_phone,encrypted_payload,f"otp:{phone}:{purpose}:{digest[:16]}"))

def consume_phone_otp(conn, phone: str, otp: str, purpose: str = "login"):
    phone = normalize_phone(phone)
    if not otp.isdigit() or len(otp) != 6:
        raise AuthenticationError("invalid or expired verification code")
    invalid = False
    result = None
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""SELECT id,otp_hash,attempts FROM phone_verifications
                WHERE phone_e164=%s AND purpose=%s AND consumed_at IS NULL AND expires_at>now()
                ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (phone,purpose))
            challenge = cur.fetchone()
            if not challenge or challenge["attempts"] >= OTP_MAX_ATTEMPTS:
                invalid = True
            elif not verify_otp_hash(challenge["otp_hash"],phone,purpose,otp):
                # Do not raise inside this transaction: that would roll back the
                # failed-attempt counter and make online guessing unlimited.
                cur.execute("UPDATE phone_verifications SET attempts=attempts+1 WHERE id=%s", (challenge["id"],))
                invalid = True
            else:
                cur.execute("UPDATE phone_verifications SET consumed_at=now() WHERE id=%s", (challenge["id"],))
                cur.execute("SELECT id,status FROM users WHERE phone_e164=%s FOR UPDATE", (phone,))
                user = cur.fetchone()
                if purpose == "signup" and user:
                    invalid = True
                elif purpose == "login" and not user:
                    invalid = True
                elif user and user["status"] != "active":
                    invalid = True
                else:
                    if not user:
                        cur.execute("""INSERT INTO users(public_id,phone_e164,phone_verified_at)
                            VALUES ('usr_' || encode(gen_random_bytes(12),'hex'),%s,now()) RETURNING id""", (phone,))
                        user_id = cur.fetchone()["id"]
                        _ensure_user_role(cur,user_id)
                        cur.execute("INSERT INTO auth_identities(user_id,provider,provider_subject) VALUES (%s,'phone_otp',%s)", (user_id,phone))
                        cur.execute("INSERT INTO user_preferences(user_id) VALUES (%s)", (user_id,))
                        cur.execute("INSERT INTO wallet_accounts(user_id) VALUES (%s)", (user_id,))
                    else:
                        user_id = user["id"]
                        cur.execute("UPDATE users SET phone_verified_at=COALESCE(phone_verified_at,now()),last_login_at=now() WHERE id=%s", (user_id,))
                    result = create_session(conn,user_id)
    if invalid or result is None:
        raise AuthenticationError("invalid or expired verification code")
    return result
