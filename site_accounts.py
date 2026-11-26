"""Account, subscription, wallet, referral and support services for the site.

This module deliberately uses a separate SQLite database from the read-only
TSETMC source database and from the materialised analytics sidecar. It never
opens or writes to the source holdings database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


SESSION_COOKIE = "rn_session"
REFERRAL_REWARD_TOMAN = 100_000
PASSWORD_ITERATIONS = 600_000

PLANS = (
    ("pro_30", "اشتراک حرفه‌ای یک‌ماهه", 30, 690_000),
    ("pro_90", "اشتراک حرفه‌ای سه‌ماهه", 90, 1_890_000),
    ("pro_180", "اشتراک حرفه‌ای شش‌ماهه", 180, 3_480_000),
    ("pro_365", "اشتراک حرفه‌ای یک‌ساله", 365, 6_600_000),
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS site_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT,
    display_name TEXT,
    email TEXT UNIQUE COLLATE NOCASE,
    email_verified INTEGER NOT NULL DEFAULT 0,
    phone TEXT UNIQUE,
    phone_verified INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK(role IN ('member','support','admin','super_admin')),
    referral_code TEXT NOT NULL UNIQUE,
    referred_by INTEGER REFERENCES site_users(id),
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_sessions_user ON site_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_site_sessions_expiry ON site_sessions(expires_at);
CREATE TABLE IF NOT EXISTS site_analytics_consents (
    user_id INTEGER PRIMARY KEY REFERENCES site_users(id) ON DELETE CASCADE,
    product_analytics INTEGER NOT NULL DEFAULT 0 CHECK(product_analytics IN (0,1)),
    consent_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
    session_hash TEXT NOT NULL,
    client_event_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    event_name TEXT NOT NULL CHECK(event_name IN
        ('page_view','click','navigation','scroll','active_time','feature_view')),
    page_path TEXT NOT NULL,
    component_id TEXT,
    element_role TEXT,
    scroll_depth_pct REAL,
    active_duration_ms INTEGER,
    occurred_at TEXT NOT NULL,
    UNIQUE(user_id,client_event_id)
);
CREATE INDEX IF NOT EXISTS idx_site_analytics_session_order
    ON site_analytics_events(user_id,session_hash,id);
CREATE INDEX IF NOT EXISTS idx_site_analytics_event_time
    ON site_analytics_events(event_name,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_analytics_page_time
    ON site_analytics_events(page_path,event_name,occurred_at DESC);
CREATE TABLE IF NOT EXISTS site_analytics_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER NOT NULL REFERENCES site_users(id),
    target_user_id INTEGER NOT NULL REFERENCES site_users(id),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_otp_challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    referral_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_otp_phone_created
    ON site_otp_challenges(phone, created_at DESC);
CREATE TABLE IF NOT EXISTS site_oauth_states (
    state_hash TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    referral_code TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_external_identities (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(provider, subject)
);
CREATE TABLE IF NOT EXISTS site_plans (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    price_toman INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS site_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES site_users(id),
    plan_slug TEXT NOT NULL,
    status TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    payment_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_subscriptions_user
    ON site_subscriptions(user_id, status, ends_at);
CREATE TABLE IF NOT EXISTS site_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES site_users(id),
    plan_slug TEXT NOT NULL,
    list_price_toman INTEGER NOT NULL,
    discount_toman INTEGER NOT NULL DEFAULT 0,
    wallet_used_toman INTEGER NOT NULL DEFAULT 0,
    amount_toman INTEGER NOT NULL,
    amount_rial INTEGER NOT NULL,
    gateway TEXT NOT NULL,
    authority TEXT UNIQUE,
    reference_id TEXT,
    status TEXT NOT NULL,
    promo_code TEXT,
    created_at TEXT NOT NULL,
    verified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_site_payments_user
    ON site_payments(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS site_promo_codes (
    code TEXT PRIMARY KEY,
    discount_percent INTEGER NOT NULL,
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    new_customer_only INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS site_promo_redemptions (
    promo_code TEXT NOT NULL REFERENCES site_promo_codes(code),
    user_id INTEGER NOT NULL REFERENCES site_users(id),
    payment_id INTEGER NOT NULL REFERENCES site_payments(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(promo_code, user_id)
);
CREATE TABLE IF NOT EXISTS site_wallet_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES site_users(id),
    amount_toman INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reference_key TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_wallet_user
    ON site_wallet_ledger(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS site_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES site_users(id),
    subject TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    assigned_to INTEGER REFERENCES site_users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_tickets_status
    ON site_tickets(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_site_tickets_user
    ON site_tickets(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS site_ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES site_tickets(id) ON DELETE CASCADE,
    sender_id INTEGER NOT NULL REFERENCES site_users(id),
    body TEXT NOT NULL,
    is_internal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_ticket_messages_ticket
    ON site_ticket_messages(ticket_id, created_at);
CREATE TABLE IF NOT EXISTS site_sector_configs (
    user_id INTEGER PRIMARY KEY REFERENCES site_users(id) ON DELETE CASCADE,
    mapping_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_user_watchlist (
    user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY(user_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_site_user_watchlist_user
    ON site_user_watchlist(user_id, added_at);
CREATE TABLE IF NOT EXISTS site_saved_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES site_users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    filter_ast_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_site_saved_filters_user
    ON site_saved_filters(user_id, created_at);

CREATE TABLE IF NOT EXISTS site_login_failures (
    identifier_hash TEXT PRIMARY KEY,
    failed_count INTEGER NOT NULL,
    locked_until TEXT,
    updated_at TEXT NOT NULL
);
"""


class AccountError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _json_request(url: str, payload: dict, headers: dict | None = None,
                  timeout: int = 20) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json",
                       "Accept": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data,
                                     headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise AccountError(502, "ارتباط با سرویس بیرونی برقرار نشد؛ تنظیمات سرویس را بررسی کنید.") from exc
    if not isinstance(parsed, dict):
        raise AccountError(502, "پاسخ سرویس بیرونی معتبر نیست.")
    return parsed


class AccountService:
    """Owns site accounts; never touches the TSETMC source database."""

    def __init__(self, database_path: str):
        self.database_path = os.path.abspath(database_path)
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.database_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init_db(self):
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            # Safe additive migration for pre-existing OAuth state databases.
            oauth_columns = {
                row["name"] for row in
                con.execute("PRAGMA table_info(site_oauth_states)").fetchall()
            }
            if "referral_code" not in oauth_columns:
                con.execute("ALTER TABLE site_oauth_states ADD COLUMN referral_code TEXT")
            con.execute("PRAGMA journal_mode=WAL")
            with con:
                for slug, name, days, price in PLANS:
                    con.execute(
                        "INSERT INTO site_plans "
                        "(slug,name,duration_days,price_toman,is_active) "
                        "VALUES (?,?,?,?,1) ON CONFLICT(slug) DO UPDATE SET "
                        "name=excluded.name,duration_days=excluded.duration_days,"
                        "price_toman=excluded.price_toman,is_active=1",
                        (slug, name, days, price))
                con.execute("UPDATE site_promo_codes SET is_active=0 WHERE code IN ('WELCOME5','WELCOME10','WELCOME20')")
                con.execute(
                    "INSERT INTO site_promo_codes "
                    "(code,discount_percent,max_uses,used_count,new_customer_only,is_active) "
                    "VALUES ('WELCOME15',15,2000,0,1,1) "
                    "ON CONFLICT(code) DO UPDATE SET discount_percent=15,"
                    "max_uses=2000,new_customer_only=1,is_active=1")
        finally:
            con.close()

    @staticmethod
    def normalize_phone(value: str) -> str:
        raw = re.sub(r"[\s().-]", "", str(value or ""))
        raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        if raw.startswith("0098"):
            raw = "+98" + raw[4:]
        elif raw.startswith("98") and len(raw) == 12:
            raw = "+" + raw
        elif raw.startswith("0") and len(raw) == 11:
            raw = "+98" + raw[1:]
        elif raw.startswith("9") and len(raw) == 10:
            raw = "+98" + raw
        if not re.fullmatch(r"\+989\d{9}", raw):
            raise AccountError(400, "شماره همراه معتبر ایران را وارد کنید.")
        return raw

    @staticmethod
    def _password_hash(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt,
            PASSWORD_ITERATIONS, dklen=32)
        return "pbkdf2_sha256$%d$%s$%s" % (
            PASSWORD_ITERATIONS,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"))

    @staticmethod
    def _verify_password(password: str, encoded: str | None) -> bool:
        if not encoded:
            return False
        try:
            algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt,
                int(rounds), dklen=len(expected))
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _token_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _wallet_balance(self, con, user_id: int) -> int:
        row = con.execute(
            "SELECT COALESCE(SUM(amount_toman),0) FROM site_wallet_ledger WHERE user_id=?",
            (user_id,)).fetchone()
        return int(row[0] or 0)

    def _active_subscription(self, con, user_id: int):
        now = iso_now()
        row = con.execute(
            "SELECT plan_slug,starts_at,ends_at,status FROM site_subscriptions "
            "WHERE user_id=? AND status='active' AND ends_at>? "
            "ORDER BY ends_at DESC LIMIT 1", (user_id, now)).fetchone()
        return dict(row) if row else None

    def _safe_user(self, con, user_id: int, csrf_token: str | None = None) -> dict:
        row = con.execute(
            "SELECT id,public_id,username,display_name,email,email_verified,"
            "phone,phone_verified,role,referral_code,must_change_password "
            "FROM site_users WHERE id=? AND is_active=1", (user_id,)).fetchone()
        if not row:
            raise AccountError(401, "حساب کاربری فعال نیست.")
        user = dict(row)
        subscription = self._active_subscription(con, user_id)
        user["subscription"] = subscription
        user["is_premium"] = bool(subscription) or user["role"] in (
            "admin", "super_admin")
        user["wallet_balance_toman"] = self._wallet_balance(con, user_id)
        user["csrf_token"] = csrf_token
        return user

    def authenticate(self, raw_session: str | None):
        if not raw_session or len(raw_session) > 256:
            return None
        token_hash = self._token_hash(raw_session)
        con = self._connect()
        try:
            row = con.execute(
                "SELECT s.user_id,s.csrf_token,s.expires_at,u.is_active "
                "FROM site_sessions s JOIN site_users u ON u.id=s.user_id "
                "WHERE s.session_hash=?", (token_hash,)).fetchone()
            if not row or not row["is_active"]:
                return None
            if (_parse_iso(row["expires_at"]) or utc_now()) <= utc_now():
                with con:
                    con.execute("DELETE FROM site_sessions WHERE session_hash=?",
                                (token_hash,))
                return None
            with con:
                con.execute("UPDATE site_sessions SET last_seen_at=? WHERE session_hash=?",
                            (iso_now(), token_hash))
            user = self._safe_user(con, row["user_id"], row["csrf_token"])
            user["session_hash"] = token_hash
            return user
        finally:
            con.close()

    def require_user(self, user):
        if not user:
            raise AccountError(401, "برای استفاده از این بخش ابتدا وارد حساب شوید.")
        return user

    @staticmethod
    def require_role(user, roles):
        if not user or user.get("role") not in set(roles):
            raise AccountError(403, "دسترسی لازم برای این عملیات را ندارید.")

    def require_premium(self, user):
        self.require_user(user)
        if not user.get("is_premium"):
            raise AccountError(402, "این قابلیت مخصوص اشتراک حرفه‌ای است.")

    def check_csrf(self, user, supplied: str | None):
        if not user:
            return
        if not supplied or not hmac.compare_digest(
                str(user.get("csrf_token") or ""), str(supplied)):
            raise AccountError(403, "درخواست معتبر نیست؛ صفحه را تازه‌سازی کنید.")

    @staticmethod
    def _validate_username(username: str):
        value = str(username or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{4,32}", value):
            raise AccountError(400, "شناسه ورود باید ۴ تا ۳۲ نویسه و شامل حروف انگلیسی، عدد، نقطه یا خط باشد.")
        return value

    @staticmethod
    def _validate_password(password: str):
        if not isinstance(password, str) or len(password) < 10 or len(password) > 256:
            raise AccountError(400, "رمز عبور باید حداقل ۱۰ نویسه داشته باشد.")
        return password

    def _create_user(self, con, username: str, password: str | None = None,
                     display_name: str | None = None, email: str | None = None,
                     phone: str | None = None, email_verified: bool = False,
                     phone_verified: bool = False, referral_code: str | None = None,
                     role: str = "member", must_change_password: bool = False):
        username = self._validate_username(username)
        if role not in ("member", "support", "admin", "super_admin"):
            raise AccountError(400, "نقش کاربری نامعتبر است.")
        if password is not None:
            self._validate_password(password)
        email = (str(email).strip().lower() if email else None)
        if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
            raise AccountError(400, "نشانی ایمیل معتبر نیست.")
        phone = self.normalize_phone(phone) if phone else None
        referred_by = None
        if referral_code:
            ref = con.execute(
                "SELECT id FROM site_users WHERE referral_code=? AND is_active=1",
                (str(referral_code).strip().upper(),)).fetchone()
            if not ref:
                raise AccountError(400, "کد دعوت معتبر نیست.")
            referred_by = ref["id"]
        public_id = "RN-" + secrets.token_hex(5).upper()
        ref_code = secrets.token_hex(4).upper()
        password_hash = self._password_hash(password) if password else None
        try:
            cur = con.execute(
                "INSERT INTO site_users (public_id,username,password_hash,display_name,"
                "email,email_verified,phone,phone_verified,role,referral_code,"
                "referred_by,must_change_password,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (public_id, username, password_hash, display_name or username,
                 email, int(email_verified), phone, int(phone_verified), role,
                 ref_code, referred_by, int(must_change_password), iso_now()))
        except sqlite3.IntegrityError as exc:
            raise AccountError(409, "شناسه، شماره یا ایمیل قبلاً ثبت شده است.") from exc
        return int(cur.lastrowid)

    def register(self, body: dict) -> int:
        username = self._validate_username(body.get("username"))
        password = self._validate_password(body.get("password"))
        display_name = str(body.get("display_name") or username).strip()[:80]
        if body.get("email"):
            raise AccountError(400, "برای اتصال Gmail از ورود با Google استفاده کنید تا مالکیت ایمیل تأیید شود.")
        if body.get("phone"):
            raise AccountError(400, "برای اتصال شماره همراه از ورود پیامکی و کد یک‌بارمصرف استفاده کنید.")
        con = self._connect()
        try:
            with con:
                return self._create_user(
                    con, username, password, display_name,
                    referral_code=body.get("referral_code"))
        finally:
            con.close()

    def _new_session(self, user_id: int, remember: bool):
        raw = secrets.token_urlsafe(40)
        csrf = secrets.token_urlsafe(24)
        ttl = 30 * 24 * 3600 if remember else 12 * 3600
        now = utc_now()
        expires = (now + timedelta(seconds=ttl)).isoformat(timespec="seconds")
        con = self._connect()
        try:
            with con:
                con.execute(
                    "INSERT INTO site_sessions(session_hash,user_id,csrf_token,"
                    "expires_at,created_at,last_seen_at) VALUES (?,?,?,?,?,?)",
                    (self._token_hash(raw), user_id, csrf, expires,
                     now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))
                con.execute("DELETE FROM site_sessions WHERE expires_at<?",
                            (now.isoformat(timespec="seconds"),))
                user = self._safe_user(con, user_id, csrf)
            return raw, user, ttl
        finally:
            con.close()

    @staticmethod
    def session_cookie(raw: str, ttl: int, secure: bool) -> str:
        value = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            SESSION_COOKIE, raw, int(ttl))
        return value + ("; Secure" if secure else "")

    @staticmethod
    def clear_session_cookie(secure: bool) -> str:
        value = "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % SESSION_COOKIE
        return value + ("; Secure" if secure else "")

    def _session_result(self, user_id: int, remember: bool, secure: bool):
        raw, user, ttl = self._new_session(user_id, remember)
        return {
            "payload": {"user": user},
            "headers": {"Set-Cookie": self.session_cookie(raw, ttl, secure)}
        }

    def _login_failure(self, identifier: str):
        key = self._token_hash(identifier.strip().lower())
        con = self._connect()
        try:
            now = utc_now()
            with con:
                row = con.execute(
                    "SELECT failed_count,locked_until FROM site_login_failures "
                    "WHERE identifier_hash=?", (key,)).fetchone()
                locked = _parse_iso(row["locked_until"]) if row else None
                if locked and locked > now:
                    raise AccountError(429, "تلاش‌های ورود زیاد بوده است؛ بعداً دوباره امتحان کنید.")
                count = (int(row["failed_count"]) if row else 0) + 1
                lock_until = (now + timedelta(minutes=15)).isoformat(timespec="seconds") if count >= 8 else None
                con.execute(
                    "INSERT INTO site_login_failures(identifier_hash,failed_count,locked_until,updated_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(identifier_hash) DO UPDATE SET "
                    "failed_count=excluded.failed_count,locked_until=excluded.locked_until,"
                    "updated_at=excluded.updated_at",
                    (key, count, lock_until, now.isoformat(timespec="seconds")))
        finally:
            con.close()

    def _clear_login_failures(self, identifier: str):
        con = self._connect()
        try:
            with con:
                con.execute("DELETE FROM site_login_failures WHERE identifier_hash=?",
                            (self._token_hash(identifier.strip().lower()),))
        finally:
            con.close()

    def login(self, body: dict) -> int:
        identifier = str(body.get("identifier") or "").strip()
        password = str(body.get("password") or "")
        if not identifier or not password:
            raise AccountError(400, "شناسه و رمز عبور را وارد کنید.")
        # Phone numbers are stored in canonical +98 format. Accept the
        # common local 09xx form as a login identifier too.
        phone_identifier = identifier
        try:
            phone_identifier = self.normalize_phone(identifier)
        except AccountError:
            pass
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id,password_hash,is_active FROM site_users "
                "WHERE username=? COLLATE NOCASE OR public_id=? COLLATE NOCASE "
                "OR (email=? COLLATE NOCASE AND email_verified=1) "
                "OR (phone=? AND phone_verified=1)",
                (identifier, identifier, identifier.lower(), phone_identifier)).fetchone()
            if not row or not row["is_active"] or not self._verify_password(password, row["password_hash"]):
                self._login_failure(identifier)
                raise AccountError(401, "شناسه یا رمز عبور اشتباه است.")
            self._clear_login_failures(identifier)
            return int(row["id"])
        finally:
            con.close()

    def _sms_send(self, phone: str, code: str):
        url = os.environ.get("SITE_SMS_API_URL", "").strip()
        token = os.environ.get("SITE_SMS_API_TOKEN", "").strip()
        if not url or not token:
            raise AccountError(503, "ورود پیامکی هنوز پیکربندی نشده است؛ کلید سرویس پیامک روی سرور لازم است.")
        if not url.lower().startswith("https://"):
            raise AccountError(503, "نشانی سرویس پیامک باید HTTPS باشد.")
        body = {
            "phone": phone,
            "code": code,
            "message": "کد ورود ره‌نگار بورس: %s" % code,
        }
        try:
            _json_request(url, body, {"Authorization": "Bearer " + token})
        except AccountError:
            raise
        except Exception as exc:
            raise AccountError(502, "ارسال پیامک ناموفق بود.") from exc

    def request_otp(self, body: dict):
        phone = self.normalize_phone(body.get("phone"))
        referral_code = str(body.get("referral_code") or "").strip().upper()[:24] or None
        con = self._connect()
        try:
            now = utc_now()
            rows = con.execute(
                "SELECT created_at FROM site_otp_challenges WHERE phone=? "
                "ORDER BY id DESC LIMIT 6", (phone,)).fetchall()
            if rows and (now - (_parse_iso(rows[0]["created_at"]) or now)).total_seconds() < 60:
                raise AccountError(429, "برای ارسال کد جدید حداقل یک دقیقه صبر کنید.")
            recent = con.execute(
                "SELECT COUNT(*) FROM site_otp_challenges WHERE phone=? AND created_at>=?",
                (phone, (now - timedelta(hours=1)).isoformat(timespec="seconds"))).fetchone()[0]
            if recent >= 5:
                raise AccountError(429, "سقف ارسال کد برای این شماره در یک ساعت پر شده است.")
            code = "%06d" % secrets.randbelow(1_000_000)
            with con:
                con.execute(
                    "INSERT INTO site_otp_challenges(phone,code_hash,referral_code,attempts,"
                    "expires_at,created_at) VALUES (?,?,?,0,?,?)",
                    (phone, self._token_hash(code), referral_code,
                     (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                     now.isoformat(timespec="seconds")))
            self._sms_send(phone, code)
            return {"sent": True, "expires_in_seconds": 300}
        finally:
            con.close()

    def _grant_trial(self, con, user_id: int):
        if con.execute(
                "SELECT 1 FROM site_subscriptions WHERE user_id=? LIMIT 1",
                (user_id,)).fetchone():
            return
        now = utc_now()
        con.execute(
            "INSERT INTO site_subscriptions(user_id,plan_slug,status,starts_at,ends_at,created_at) "
            "VALUES (?,'trial_3','active',?,?,?)",
            (user_id, now.isoformat(timespec="seconds"),
             (now + timedelta(days=3)).isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds")))

    def verify_otp(self, body: dict) -> int:
        phone = self.normalize_phone(body.get("phone"))
        code = re.sub(r"\D", "", str(body.get("code") or ""))
        if not re.fullmatch(r"\d{6}", code):
            raise AccountError(400, "کد شش‌رقمی را وارد کنید.")
        con = self._connect()
        try:
            now = utc_now()
            row = con.execute(
                "SELECT * FROM site_otp_challenges WHERE phone=? ORDER BY id DESC LIMIT 1",
                (phone,)).fetchone()
            if not row or (_parse_iso(row["expires_at"]) or now) <= now:
                raise AccountError(400, "کد منقضی شده است؛ کد جدید بگیرید.")
            if row["attempts"] >= 5:
                raise AccountError(429, "تعداد تلاش مجاز تمام شده است.")
            if not hmac.compare_digest(row["code_hash"], self._token_hash(code)):
                with con:
                    con.execute("UPDATE site_otp_challenges SET attempts=attempts+1 WHERE id=?",
                                (row["id"],))
                raise AccountError(401, "کد واردشده صحیح نیست.")
            with con:
                con.execute("DELETE FROM site_otp_challenges WHERE phone=?", (phone,))
                user = con.execute(
                    "SELECT id FROM site_users WHERE phone=?", (phone,)).fetchone()
                if user:
                    user_id = int(user["id"])
                    con.execute("UPDATE site_users SET phone_verified=1 WHERE id=?",
                                (user_id,))
                else:
                    username = "u" + secrets.token_hex(5)
                    user_id = self._create_user(
                        con, username, display_name="کاربر " + phone[-4:],
                        phone=phone, phone_verified=True,
                        referral_code=row["referral_code"])
                self._grant_trial(con, user_id)
                return user_id
        finally:
            con.close()

    def google_start(self, base_url: str, referral_code: str | None = None):
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        callback = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        if not client_id or not callback:
            raise AccountError(503, "ورود با گوگل هنوز پیکربندی نشده است.")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        now = utc_now()
        con = self._connect()
        try:
            with con:
                con.execute(
                    "INSERT INTO site_oauth_states(state_hash,nonce,code_verifier,referral_code,expires_at,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (self._token_hash(state), nonce, verifier,
                     (str(referral_code or "").strip().upper() or None),
                     (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
                     now.isoformat(timespec="seconds")))
                con.execute("DELETE FROM site_oauth_states WHERE expires_at<?",
                            (now.isoformat(timespec="seconds"),))
        finally:
            con.close()
        query = urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": callback,
            "response_type": "code", "scope": "openid email profile",
            "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "select_account",
        })
        return "https://accounts.google.com/o/oauth2/v2/auth?" + query

    def google_callback(self, params: dict) -> int:
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        if not code or not state:
            raise AccountError(400, "پاسخ ورود گوگل ناقص است.")
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        callback = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        if not client_id or not client_secret or not callback:
            raise AccountError(503, "تنظیمات OAuth گوگل کامل نیست.")
        con = self._connect()
        try:
            state_row = con.execute(
                "SELECT * FROM site_oauth_states WHERE state_hash=?",
                (self._token_hash(state),)).fetchone()
            if not state_row or (_parse_iso(state_row["expires_at"]) or utc_now()) <= utc_now():
                raise AccountError(400, "درخواست ورود گوگل منقضی یا نامعتبر است.")
            with con:
                con.execute("DELETE FROM site_oauth_states WHERE state_hash=?",
                            (self._token_hash(state),))
        finally:
            con.close()

        # Google's OAuth token endpoint requires URL-encoded form data,
        # not the JSON body used by the ZarinPal REST API.
        token_form = urllib.parse.urlencode({
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": callback,
            "grant_type": "authorization_code",
            "code_verifier": state_row["code_verifier"],
        }).encode("ascii")
        token_request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_form,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(token_request, timeout=20) as response:
                token_data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise AccountError(502, "تبادل کد ورود گوگل انجام نشد.") from exc
        if not isinstance(token_data, dict):
            raise AccountError(502, "پاسخ توکن گوگل معتبر نیست.")
        id_token = token_data.get("id_token")
        if not id_token:
            raise AccountError(401, "گوگل توکن هویت برنگرداند.")
        try:
            with urllib.request.urlopen(
                    "https://oauth2.googleapis.com/tokeninfo?id_token=" +
                    urllib.parse.quote(str(id_token), safe=""), timeout=15) as response:
                claims = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise AccountError(502, "اعتبارسنجی پاسخ گوگل انجام نشد.") from exc
        issuer = claims.get("iss")
        if issuer not in ("accounts.google.com", "https://accounts.google.com"):
            raise AccountError(401, "صادرکننده توکن گوگل معتبر نیست.")
        if claims.get("aud") != client_id or claims.get("nonce") != state_row["nonce"]:
            raise AccountError(401, "توکن گوگل با درخواست ورود مطابقت ندارد.")
        if int(claims.get("exp") or 0) <= int(time.time()):
            raise AccountError(401, "توکن گوگل منقضی شده است.")
        if str(claims.get("email_verified", "")).lower() not in ("true", "1"):
            raise AccountError(401, "ایمیل گوگل تأیید نشده است.")
        subject = str(claims.get("sub") or "").strip()
        email = str(claims.get("email") or "").strip().lower()
        if not subject or not email:
            raise AccountError(401, "اطلاعات حساب گوگل کامل نیست.")

        con = self._connect()
        try:
            with con:
                linked = con.execute(
                    "SELECT user_id FROM site_external_identities "
                    "WHERE provider='google' AND subject=?", (subject,)).fetchone()
                if linked:
                    user_id = int(linked["user_id"])
                else:
                    existing = con.execute(
                        "SELECT id,email_verified FROM site_users WHERE email=? COLLATE NOCASE",
                        (email,)).fetchone()
                    if existing and not existing["email_verified"]:
                        raise AccountError(
                            409,
                            "این ایمیل قبلاً بدون تأیید به حساب دیگری متصل شده است؛ ابتدا با شناسه همان حساب وارد شوید و با پشتیبانی تماس بگیرید.",
                        )
                    if existing:
                        user_id = int(existing["id"])
                    else:
                        base = re.sub(r"[^a-z0-9_.-]", "", email.split("@")[0].lower())[:20]
                        username = (base or "google") + "_" + secrets.token_hex(3)
                        user_id = self._create_user(
                            con, username, display_name=claims.get("name") or base,
                            email=email, email_verified=True,
                            referral_code=state_row["referral_code"])
                        self._grant_trial(con, user_id)
                    con.execute(
                        "INSERT INTO site_external_identities(provider,subject,user_id,created_at) "
                        "VALUES ('google',?,?,?)",
                        (subject, user_id, iso_now()))
                return user_id
        finally:
            con.close()

    def _promotion(self, con, user_id: int, code: str | None, list_price: int):
        if not code:
            return None, 0
        normalized = str(code).strip().upper()
        row = con.execute(
            "SELECT * FROM site_promo_codes WHERE code=? AND is_active=1",
            (normalized,)).fetchone()
        if not row:
            raise AccountError(400, "کد تشویقی معتبر نیست.")
        if row["expires_at"] and (_parse_iso(row["expires_at"]) or utc_now()) <= utc_now():
            raise AccountError(400, "مهلت استفاده از کد تشویقی تمام شده است.")
        if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
            raise AccountError(400, "ظرفیت این طرح تشویقی تکمیل شده است.")
        if con.execute(
                "SELECT 1 FROM site_promo_redemptions WHERE promo_code=? AND user_id=?",
                (normalized, user_id)).fetchone():
            raise AccountError(409, "این کد تشویقی را قبلاً استفاده کرده‌اید.")
        if row["new_customer_only"] and con.execute(
                "SELECT 1 FROM site_payments WHERE user_id=? AND status='paid' LIMIT 1",
                (user_id,)).fetchone():
            raise AccountError(400, "این کد فقط برای اولین خرید است.")
        discount = int(list_price * int(row["discount_percent"]) / 100)
        return normalized, min(discount, list_price)

    def _zarinpal_base(self):
        sandbox = os.environ.get("ZARINPAL_SANDBOX", "").lower() in ("1", "true", "yes")
        if sandbox:
            return ("https://sandbox.zarinpal.com/pg/v4/payment",
                    "https://sandbox.zarinpal.com/pg/StartPay/")
        return ("https://api.zarinpal.com/pg/v4/payment",
                "https://www.zarinpal.com/pg/StartPay/")

    def create_checkout(self, user: dict, body: dict, base_url: str):
        slug = str(body.get("plan_slug") or "")
        use_wallet = bool(body.get("use_wallet", True))
        promo_code = body.get("promo_code")
        con = self._connect()
        payment_id = None
        authority = None
        try:
            with con:
                plan = con.execute(
                    "SELECT * FROM site_plans WHERE slug=? AND is_active=1",
                    (slug,)).fetchone()
                if not plan:
                    raise AccountError(400, "طرح اشتراک انتخاب‌شده معتبر نیست.")
                list_price = int(plan["price_toman"])
                normalized_promo, discount = self._promotion(
                    con, user["id"], promo_code, list_price)
                after_discount = max(0, list_price - discount)
                wallet_balance = self._wallet_balance(con, user["id"])
                wallet_used = min(wallet_balance, after_discount) if use_wallet else 0
                amount = after_discount - wallet_used
                order_id = secrets.token_hex(16)
                cur = con.execute(
                    "INSERT INTO site_payments(order_id,user_id,plan_slug,list_price_toman,"
                    "discount_toman,wallet_used_toman,amount_toman,amount_rial,gateway,"
                    "status,promo_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (order_id, user["id"], slug, list_price, discount, wallet_used,
                     amount, amount * 10, "zarinpal", normalized_promo, iso_now()))
                payment_id = int(cur.lastrowid)
                if wallet_used:
                    con.execute(
                        "INSERT INTO site_wallet_ledger(user_id,amount_toman,kind,reference_key,"
                        "description,created_at) VALUES (?,?, 'subscription_debit',?,?,?)",
                        (user["id"], -wallet_used, "checkout:" + order_id,
                         "استفاده از اعتبار کیف پول برای خرید اشتراک", iso_now()))
                if amount == 0:
                    self._settle_payment(con, payment_id, "wallet", None)
                    return {"paid_with_wallet": True, "order_id": order_id}
        except Exception:
            if payment_id and authority is None:
                self._refund_wallet_for_payment(payment_id)
            raise
        finally:
            con.close()

        merchant = os.environ.get("ZARINPAL_MERCHANT_ID", "").strip()
        public_base = os.environ.get("SITE_PUBLIC_BASE_URL", "").strip().rstrip("/")
        callback = public_base + "/api/v1/payments/zarinpal/callback"
        if not merchant or not public_base.startswith("https://"):
            self._refund_wallet_for_payment(payment_id)
            self._mark_payment_failed(payment_id)
            raise AccountError(503, "درگاه پرداخت آماده نیست؛ شناسه پذیرنده و دامنه HTTPS باید روی سرور تنظیم شوند.")
        api_base, pay_base = self._zarinpal_base()
        con = self._connect()
        try:
            payment = con.execute(
                "SELECT * FROM site_payments WHERE id=?", (payment_id,)).fetchone()
            payload = {
                "merchant_id": merchant,
                "amount": int(payment["amount_rial"]),
                "currency": "IRR",
                "callback_url": callback,
                "description": "خرید اشتراک ره‌نگار بورس",
                "metadata": {"mobile": user.get("phone"), "email": user.get("email"),
                             "order_id": payment["order_id"]},
            }
            result = _json_request(api_base + "/request.json", payload)
            data = result.get("data") or {}
            if int(data.get("code") or -1) != 100 or not data.get("authority"):
                raise AccountError(502, "درگاه پرداخت درخواست را نپذیرفت.")
            authority = str(data["authority"])
            with con:
                con.execute(
                    "UPDATE site_payments SET authority=? WHERE id=? AND status='pending'",
                    (authority, payment_id))
            return {"checkout_url": pay_base + urllib.parse.quote(authority, safe=""),
                    "order_id": payment["order_id"]}
        except Exception:
            self._refund_wallet_for_payment(payment_id)
            self._mark_payment_failed(payment_id)
            raise
        finally:
            con.close()

    def _mark_payment_failed(self, payment_id: int):
        con = self._connect()
        try:
            with con:
                con.execute(
                    "UPDATE site_payments SET status='failed' "
                    "WHERE id=? AND status='pending'", (payment_id,))
        finally:
            con.close()

    def _refund_wallet_for_payment(self, payment_id: int):
        con = self._connect()
        try:
            with con:
                row = con.execute(
                    "SELECT user_id,order_id,wallet_used_toman FROM site_payments WHERE id=?",
                    (payment_id,)).fetchone()
                if row and row["wallet_used_toman"]:
                    con.execute(
                        "INSERT OR IGNORE INTO site_wallet_ledger(user_id,amount_toman,kind,"
                        "reference_key,description,created_at) VALUES (?,?, 'payment_refund',?,?,?)",
                        (row["user_id"], row["wallet_used_toman"],
                         "refund:" + row["order_id"],
                         "بازگشت اعتبار خرید ناموفق", iso_now()))
        finally:
            con.close()

    def _settle_payment(self, con, payment_id: int, status: str, reference_id: str | None):
        payment = con.execute(
            "SELECT * FROM site_payments WHERE id=?", (payment_id,)).fetchone()
        if not payment or payment["status"] == "paid":
            return
        now = utc_now()
        active = self._active_subscription(con, payment["user_id"])
        start = _parse_iso(active["ends_at"]) if active else None
        if not start or start < now:
            start = now
        plan = con.execute(
            "SELECT duration_days FROM site_plans WHERE slug=?",
            (payment["plan_slug"],)).fetchone()
        if not plan:
            raise AccountError(500, "طرح خریداری‌شده در پایگاه حساب‌ها پیدا نشد.")
        end = start + timedelta(days=int(plan["duration_days"]))
        con.execute(
            "INSERT INTO site_subscriptions(user_id,plan_slug,status,starts_at,ends_at,"
            "payment_id,created_at) VALUES (?,?, 'active',?,?,?,?)",
            (payment["user_id"], payment["plan_slug"],
             start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"),
             payment_id, now.isoformat(timespec="seconds")))
        con.execute(
            "UPDATE site_payments SET status='paid',reference_id=?,verified_at=? WHERE id=?",
            (reference_id, now.isoformat(timespec="seconds"), payment_id))
        if payment["promo_code"]:
            con.execute(
                "INSERT OR IGNORE INTO site_promo_redemptions(promo_code,user_id,payment_id,created_at) "
                "VALUES (?,?,?,?)",
                (payment["promo_code"], payment["user_id"], payment_id,
                 now.isoformat(timespec="seconds")))
            con.execute(
                "UPDATE site_promo_codes SET used_count=used_count+1 WHERE code=?",
                (payment["promo_code"],))
        invitee = con.execute(
            "SELECT referred_by FROM site_users WHERE id=?",
            (payment["user_id"],)).fetchone()
        if invitee and invitee["referred_by"]:
            inviter = int(invitee["referred_by"])
            con.execute(
                "INSERT OR IGNORE INTO site_wallet_ledger(user_id,amount_toman,kind,"
                "reference_key,description,created_at) VALUES (?,?, 'referral_reward',?,?,?)",
                (inviter, REFERRAL_REWARD_TOMAN,
                 "referral:" + str(payment["user_id"]),
                 "پاداش دعوت کاربر پس از اولین خرید موفق", now.isoformat(timespec="seconds")))

    def verify_zarinpal(self, params: dict):
        authority = (params.get("Authority") or params.get("authority") or [""])[0]
        status = (params.get("Status") or params.get("status") or [""])[0]
        if not authority:
            raise AccountError(400, "شناسه پرداخت در callback موجود نیست.")
        con = self._connect()
        try:
            payment = con.execute(
                "SELECT * FROM site_payments WHERE authority=?", (authority,)).fetchone()
            if not payment:
                raise AccountError(404, "سفارش پرداخت پیدا نشد.")
            if payment["status"] == "paid":
                return {"success": True, "reference_id": payment["reference_id"]}
            if str(status).upper() != "OK":
                self._mark_payment_failed(int(payment["id"]))
                self._refund_wallet_for_payment(int(payment["id"]))
                return {"success": False}
        finally:
            con.close()

        merchant = os.environ.get("ZARINPAL_MERCHANT_ID", "").strip()
        if not merchant:
            raise AccountError(503, "درگاه پرداخت برای تأیید پیکربندی نشده است.")
        api_base, _pay_base = self._zarinpal_base()
        response = _json_request(
            api_base + "/verify.json",
            {"merchant_id": merchant, "amount": int(payment["amount_rial"]),
             "authority": authority})
        data = response.get("data") or {}
        code = int(data.get("code") or -1)
        if code not in (100, 101):
            self._mark_payment_failed(int(payment["id"]))
            self._refund_wallet_for_payment(int(payment["id"]))
            return {"success": False}
        reference = str(data.get("ref_id") or "")
        con = self._connect()
        try:
            with con:
                self._settle_payment(con, int(payment["id"]), "paid", reference)
        finally:
            con.close()
        return {"success": True, "reference_id": reference}

    def wallet(self, user_id: int):
        con = self._connect()
        try:
            balance = self._wallet_balance(con, user_id)
            rows = con.execute(
                "SELECT amount_toman,kind,description,created_at "
                "FROM site_wallet_ledger WHERE user_id=? "
                "ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
            return {"balance_toman": balance, "transactions": [dict(r) for r in rows]}
        finally:
            con.close()

    def create_ticket(self, user_id: int, body: dict):
        subject = str(body.get("subject") or "").strip()
        message = str(body.get("message") or "").strip()
        if not 5 <= len(subject) <= 120 or not 10 <= len(message) <= 5000:
            raise AccountError(400, "عنوان باید ۵ تا ۱۲۰ و متن پیام ۱۰ تا ۵۰۰۰ نویسه باشد.")
        priority = str(body.get("priority") or "normal")
        if priority not in ("low", "normal", "high"):
            priority = "normal"
        con = self._connect()
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO site_tickets(user_id,subject,priority,status,created_at,updated_at) "
                    "VALUES (?,?,?,'open',?,?)",
                    (user_id, subject, priority, iso_now(), iso_now()))
                ticket_id = int(cur.lastrowid)
                con.execute(
                    "INSERT INTO site_ticket_messages(ticket_id,sender_id,body,is_internal,created_at) "
                    "VALUES (?,?,?,0,?)", (ticket_id, user_id, message, iso_now()))
            return {"id": ticket_id, "status": "open"}
        finally:
            con.close()

    def list_tickets(self, user: dict):
        con = self._connect()
        try:
            staff = user["role"] in ("support", "admin", "super_admin")
            if staff:
                rows = con.execute(
                    "SELECT t.id,t.subject,t.priority,t.status,t.assigned_to,t.created_at,"
                    "t.updated_at,u.public_id,u.display_name AS customer_name,"
                    "(SELECT body FROM site_ticket_messages m WHERE m.ticket_id=t.id "
                    "AND m.is_internal=0 ORDER BY m.id DESC LIMIT 1) AS last_message "
                    "FROM site_tickets t JOIN site_users u ON u.id=t.user_id "
                    "ORDER BY t.updated_at DESC LIMIT 200").fetchall()
            else:
                rows = con.execute(
                    "SELECT t.id,t.subject,t.priority,t.status,t.created_at,t.updated_at,"
                    "(SELECT body FROM site_ticket_messages m WHERE m.ticket_id=t.id "
                    "AND m.is_internal=0 ORDER BY m.id DESC LIMIT 1) AS last_message "
                    "FROM site_tickets t WHERE t.user_id=? "
                    "ORDER BY t.updated_at DESC LIMIT 100", (user["id"],)).fetchall()
            return {"tickets": [dict(r) for r in rows]}
        finally:
            con.close()

    def ticket_messages(self, user: dict, ticket_id: int):
        con = self._connect()
        try:
            ticket = con.execute(
                "SELECT user_id FROM site_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not ticket:
                raise AccountError(404, "تیکت پیدا نشد.")
            staff = user["role"] in ("support", "admin", "super_admin")
            if not staff and int(ticket["user_id"]) != int(user["id"]):
                raise AccountError(403, "به این تیکت دسترسی ندارید.")
            rows = con.execute(
                "SELECT m.id,m.sender_id,u.display_name,m.body,m.is_internal,m.created_at "
                "FROM site_ticket_messages m JOIN site_users u ON u.id=m.sender_id "
                "WHERE m.ticket_id=? AND (?=1 OR m.is_internal=0) "
                "ORDER BY m.id",
                (ticket_id, int(staff))).fetchall()
            return {"messages": [dict(r) for r in rows]}
        finally:
            con.close()

    def ticket_reply(self, user: dict, ticket_id: int, body: dict):
        message = str(body.get("message") or "").strip()
        if not 2 <= len(message) <= 5000:
            raise AccountError(400, "متن پاسخ باید بین ۲ تا ۵۰۰۰ نویسه باشد.")
        internal = bool(body.get("internal", False))
        staff = user["role"] in ("support", "admin", "super_admin")
        if internal and not staff:
            raise AccountError(403, "یادداشت داخلی فقط برای تیم پشتیبانی است.")
        con = self._connect()
        try:
            with con:
                ticket = con.execute(
                    "SELECT user_id,status FROM site_tickets WHERE id=?",
                    (ticket_id,)).fetchone()
                if not ticket:
                    raise AccountError(404, "تیکت پیدا نشد.")
                if not staff and int(ticket["user_id"]) != int(user["id"]):
                    raise AccountError(403, "به این تیکت دسترسی ندارید.")
                con.execute(
                    "INSERT INTO site_ticket_messages(ticket_id,sender_id,body,is_internal,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (ticket_id, user["id"], message, int(internal), iso_now()))
                new_status = "waiting_customer" if staff else "waiting_support"
                if ticket["status"] == "closed" and not staff:
                    new_status = "open"
                con.execute(
                    "UPDATE site_tickets SET status=?,updated_at=? WHERE id=?",
                    (new_status, iso_now(), ticket_id))
            return {"sent": True}
        finally:
            con.close()

    def ticket_update(self, user: dict, ticket_id: int, body: dict):
        self.require_role(user, ("support", "admin", "super_admin"))
        status = body.get("status")
        assigned = body.get("assigned_to")
        con = self._connect()
        try:
            with con:
                if not con.execute("SELECT 1 FROM site_tickets WHERE id=?",
                                   (ticket_id,)).fetchone():
                    raise AccountError(404, "تیکت پیدا نشد.")
                if status is not None:
                    if status not in ("open", "waiting_support", "waiting_customer", "closed"):
                        raise AccountError(400, "وضعیت تیکت نامعتبر است.")
                    con.execute("UPDATE site_tickets SET status=?,updated_at=? WHERE id=?",
                                (status, iso_now(), ticket_id))
                if assigned is not None:
                    if assigned == "":
                        assigned_id = None
                    else:
                        staff = con.execute(
                            "SELECT id FROM site_users WHERE id=? AND role IN ('support','admin','super_admin') "
                            "AND is_active=1", (int(assigned),)).fetchone()
                        if not staff:
                            raise AccountError(400, "مسئول انتخاب‌شده عضو فعال تیم پشتیبانی نیست.")
                        assigned_id = int(staff["id"])
                    con.execute(
                        "UPDATE site_tickets SET assigned_to=?,updated_at=? WHERE id=?",
                        (assigned_id, iso_now(), ticket_id))
            return {"updated": True}
        finally:
            con.close()

    def staff_list(self):
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id,public_id,username,display_name,role,is_active,created_at "
                "FROM site_users WHERE role IN ('support','admin','super_admin') "
                "ORDER BY role,created_at").fetchall()
            return {"staff": [dict(r) for r in rows]}
        finally:
            con.close()

    def create_staff(self, body: dict):
        username = self._validate_username(body.get("username"))
        password = self._validate_password(body.get("temporary_password"))
        role = str(body.get("role") or "")
        if role not in ("admin", "support"):
            raise AccountError(400, "از این بخش فقط ادمین یا پشتیبان ساخته می‌شود.")
        con = self._connect()
        try:
            with con:
                user_id = self._create_user(
                    con, username, password,
                    display_name=str(body.get("display_name") or username)[:80],
                    role=role, must_change_password=True)
                row = con.execute(
                    "SELECT public_id,username,display_name,role FROM site_users WHERE id=?",
                    (user_id,)).fetchone()
                return {"staff": dict(row), "temporary_password_set": True}
        finally:
            con.close()

    def change_staff_role(self, target_id: int, body: dict):
        role = str(body.get("role") or "")
        if role not in ("member", "support", "admin", "super_admin"):
            raise AccountError(400, "نقش نامعتبر است.")
        con = self._connect()
        try:
            with con:
                row = con.execute(
                    "SELECT role FROM site_users WHERE id=?", (target_id,)).fetchone()
                if not row:
                    raise AccountError(404, "کاربر پیدا نشد.")
                if row["role"] == "super_admin" and role != "super_admin":
                    count = con.execute(
                        "SELECT COUNT(*) FROM site_users WHERE role='super_admin' AND is_active=1"
                    ).fetchone()[0]
                    if count <= 1:
                        raise AccountError(409, "آخرین سوپرادمین قابل تنزل نقش نیست.")
                con.execute("UPDATE site_users SET role=? WHERE id=?",
                            (role, target_id))
                if role == "member":
                    con.execute("UPDATE site_users SET must_change_password=0 WHERE id=?",
                                (target_id,))
                con.execute("DELETE FROM site_sessions WHERE user_id=?", (target_id,))
            return {"updated": True}
        finally:
            con.close()

    def users_list(self, query: str = ""):
        q = str(query or "").strip()[:80]
        con = self._connect()
        try:
            if q:
                pattern = "%" + q + "%"
                rows = con.execute(
                    "SELECT public_id,username,display_name,email,phone,role,is_active,created_at "
                    "FROM site_users WHERE username LIKE ? OR public_id LIKE ? "
                    "OR display_name LIKE ? OR email LIKE ? ORDER BY id DESC LIMIT 100",
                    (pattern, pattern, pattern, pattern)).fetchall()
            else:
                rows = con.execute(
                    "SELECT public_id,username,display_name,email,phone,role,is_active,created_at "
                    "FROM site_users ORDER BY id DESC LIMIT 100").fetchall()
            return {"users": [dict(r) for r in rows]}
        finally:
            con.close()

    def bootstrap_superadmin(self, user: dict, body: dict):
        key = os.environ.get("SITE_BOOTSTRAP_ADMIN_KEY", "")
        supplied = str(body.get("bootstrap_key") or "")
        if not key or not hmac.compare_digest(key, supplied):
            raise AccountError(403, "کلید راه‌اندازی سوپرادمین معتبر نیست.")
        con = self._connect()
        try:
            with con:
                count = con.execute(
                    "SELECT COUNT(*) FROM site_users WHERE role='super_admin' AND is_active=1"
                ).fetchone()[0]
                if count:
                    raise AccountError(409, "سوپرادمین قبلاً راه‌اندازی شده است.")
                con.execute("UPDATE site_users SET role='super_admin' WHERE id=?",
                            (user["id"],))
                con.execute("DELETE FROM site_sessions WHERE user_id=?", (user["id"],))
            return {"bootstrapped": True}
        finally:
            con.close()


    def watchlist_symbols(self, user_id: int):
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT symbol FROM site_user_watchlist WHERE user_id=? ORDER BY added_at ASC",
                (user_id,)).fetchall()
            return [row["symbol"] for row in rows]
        finally:
            con.close()

    def watchlist_add_symbol(self, user_id: int, symbol: str):
        clean = str(symbol or "").strip()
        if not clean or len(clean) > 64:
            raise AccountError(400, "نماد معتبر نیست.")
        con = self._connect()
        try:
            with con:
                count = con.execute(
                    "SELECT COUNT(*) FROM site_user_watchlist WHERE user_id=?",
                    (user_id,)).fetchone()[0]
                if count >= 100:
                    raise AccountError(400, "حداکثر ۱۰۰ نماد می‌توانید در دیده‌بان داشته باشید.")
                con.execute(
                    "INSERT OR IGNORE INTO site_user_watchlist(user_id,symbol,added_at) VALUES (?,?,?)",
                    (user_id, clean, iso_now()))
            return {"ok": True, "symbol": clean}
        finally:
            con.close()

    def watchlist_remove_symbol(self, user_id: int, symbol: str):
        con = self._connect()
        try:
            with con:
                con.execute(
                    "DELETE FROM site_user_watchlist WHERE user_id=? AND symbol=?",
                    (user_id, str(symbol or "").strip()))
            return {"ok": True}
        finally:
            con.close()

    def saved_filters_list(self, user_id: int):
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id,name,filter_ast_json FROM site_saved_filters WHERE user_id=? ORDER BY id ASC",
                (user_id,)).fetchall()
            return {"filters": [dict(row) for row in rows]}
        finally:
            con.close()

    def saved_filter_create(self, user_id: int, body: dict):
        name = str(body.get("name") or "").strip()
        ast = body.get("filter_ast")
        if not name or len(name) > 80:
            raise AccountError(400, "نام فیلتر باید ۱ تا ۸۰ نویسه باشد.")
        if not isinstance(ast, dict):
            raise AccountError(400, "ساختار فیلتر معتبر نیست.")
        encoded = json.dumps(ast, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 16_000:
            raise AccountError(413, "حجم فیلتر ذخیره‌شده بیش از حد مجاز است.")
        con = self._connect()
        try:
            with con:
                count = con.execute(
                    "SELECT COUNT(*) FROM site_saved_filters WHERE user_id=?",
                    (user_id,)).fetchone()[0]
                if count >= 100:
                    raise AccountError(400, "حداکثر ۱۰۰ فیلتر می‌توانید ذخیره کنید.")
                cur = con.execute(
                    "INSERT INTO site_saved_filters(user_id,name,filter_ast_json,created_at) VALUES (?,?,?,?)",
                    (user_id, name, encoded, iso_now()))
            return {"id": cur.lastrowid, "name": name}
        finally:
            con.close()

    def get_sector_mapping(self, user_id: int):
        con = self._connect()
        try:
            row = con.execute(
                "SELECT mapping_json,updated_at FROM site_sector_configs WHERE user_id=?",
                (user_id,)).fetchone()
            if not row:
                return {"mapping": {}, "updated_at": None}
            try:
                mapping = json.loads(row["mapping_json"])
            except ValueError:
                mapping = {}
            return {"mapping": mapping, "updated_at": row["updated_at"]}
        finally:
            con.close()

    def save_sector_mapping(self, user: dict, body: dict):
        self.require_premium(user)
        mapping = body.get("mapping")
        if not isinstance(mapping, dict) or len(mapping) > 300:
            raise AccountError(400, "ساختار دسته‌بندی معتبر نیست.")
        clean = {}
        for code, label in mapping.items():
            code = str(code).strip()
            label = str(label).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code):
                raise AccountError(400, "کد گروه معتبر نیست.")
            if not label or len(label) > 80:
                raise AccountError(400, "نام گروه باید ۱ تا ۸۰ نویسه باشد.")
            clean[code] = label
        con = self._connect()
        try:
            with con:
                con.execute(
                    "INSERT INTO site_sector_configs(user_id,mapping_json,updated_at) "
                    "VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "mapping_json=excluded.mapping_json,updated_at=excluded.updated_at",
                    (user["id"], json.dumps(clean, ensure_ascii=False), iso_now()))
            return {"saved": True, "mapping": clean}
        finally:
            con.close()


    # -- Consent-aware product analytics ----------------------------------
    @staticmethod
    def _analytics_page_path(value):
        """Accept only a clean app route; never retain query strings or fragments."""
        if not isinstance(value, str):
            raise AccountError(400, "مسیر صفحه معتبر نیست.")
        raw = value.strip()
        parsed = urllib.parse.urlsplit(raw)
        if (
            not raw.startswith("/")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or not re.fullmatch(r"/[\w./:-]{1,300}", parsed.path, re.UNICODE)
        ):
            raise AccountError(400, "مسیر صفحه باید نسبی و بدون query یا fragment باشد.")
        segments = []
        for segment in parsed.path.split("/"):
            if segment in (".", ".."):
                raise AccountError(400, "مسیر صفحه معتبر نیست.")
            if (segment.isdigit() and len(segment) >= 4) or re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", segment
            ):
                segments.append(":id")
            else:
                segments.append(segment)
        return "/".join(segments)[:300]

    @classmethod
    def _clean_analytics_event(cls, event):
        if not isinstance(event, dict):
            raise AccountError(400, "رویداد تحلیلی معتبر نیست.")
        event_id = str(event.get("client_event_id") or "").lower()
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            event_id,
        ):
            raise AccountError(400, "شناسه رویداد معتبر نیست.")
        event_name = event.get("event_name")
        allowed = {"page_view", "click", "navigation", "scroll", "active_time", "feature_view"}
        if event_name not in allowed:
            raise AccountError(400, "نوع رویداد پشتیبانی نمی‌شود.")
        page_path = cls._analytics_page_path(event.get("page_path"))
        component_id = event.get("component_id")
        if component_id is not None and (
            not isinstance(component_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", component_id)
        ):
            raise AccountError(400, "شناسه جزء صفحه معتبر نیست.")
        element_role = event.get("element_role")
        if element_role is not None and element_role not in {
            "button", "link", "tab", "navigation", "menuitem", "other"
        }:
            raise AccountError(400, "نوع جزء صفحه معتبر نیست.")

        scroll = event.get("scroll_depth_pct")
        if scroll is not None:
            if (
                isinstance(scroll, bool)
                or not isinstance(scroll, (int, float))
                or not 0 <= scroll <= 100
                or event_name != "scroll"
            ):
                raise AccountError(400, "درصد اسکرول معتبر نیست.")
            scroll = float(scroll)

        active = event.get("active_duration_ms")
        if active is not None:
            if (
                isinstance(active, bool)
                or not isinstance(active, int)
                or not 0 <= active <= 180_000
                or event_name != "active_time"
            ):
                raise AccountError(400, "مدت فعالیت معتبر نیست.")

        # Extra browser fields are deliberately discarded. No text, input values,
        # raw URLs, query strings, IP addresses or user-agent strings are retained.
        return {
            "client_event_id": event_id,
            "event_name": event_name,
            "page_path": page_path,
            "component_id": component_id,
            "element_role": element_role,
            "scroll_depth_pct": scroll,
            "active_duration_ms": active,
        }

    def analytics_consent(self, user):
        self.require_user(user)
        con = self._connect()
        try:
            row = con.execute(
                """SELECT product_analytics,consent_version,updated_at
                   FROM site_analytics_consents WHERE user_id=?""",
                (user["id"],),
            ).fetchone()
            if not row:
                return {
                    "product_analytics": False,
                    "has_choice": False,
                    "consent_version": None,
                    "updated_at": None,
                }
            return {
                "product_analytics": bool(row["product_analytics"]),
                "has_choice": True,
                "consent_version": row["consent_version"],
                "updated_at": row["updated_at"],
            }
        finally:
            con.close()

    def set_analytics_consent(self, user, product_analytics):
        self.require_user(user)
        if not isinstance(product_analytics, bool):
            raise AccountError(400, "وضعیت رضایت باید درست یا نادرست باشد.")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """INSERT INTO site_analytics_consents
                   (user_id,product_analytics,consent_version,updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     product_analytics=excluded.product_analytics,
                     consent_version=excluded.consent_version,
                     updated_at=excluded.updated_at""",
                (user["id"], int(product_analytics), "v1", iso_now()),
            )
            deleted = 0
            if not product_analytics:
                cur = con.execute(
                    "DELETE FROM site_analytics_events WHERE user_id=?",
                    (user["id"],),
                )
                deleted = cur.rowcount
            con.commit()
            result = self.analytics_consent(user)
            result["deleted_events"] = deleted
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def record_analytics_events(self, user, events):
        self.require_user(user)
        session_hash = str(user.get("session_hash") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", session_hash):
            raise AccountError(401, "نشست معتبر برای ثبت رویداد پیدا نشد.")
        if not isinstance(events, list) or not 1 <= len(events) <= 50:
            raise AccountError(400, "هر بسته باید بین ۱ تا ۵۰ رویداد داشته باشد.")
        clean = [self._clean_analytics_event(event) for event in events]
        con = self._connect()
        inserted = 0
        duplicates = 0
        try:
            # Serialize ingestion against consent revocation. If revocation wins,
            # this batch observes disabled consent; if ingestion wins, revocation
            # deletes the newly inserted rows immediately afterwards.
            con.execute("BEGIN IMMEDIATE")
            consent = con.execute(
                "SELECT product_analytics FROM site_analytics_consents WHERE user_id=?",
                (user["id"],),
            ).fetchone()
            if not consent or not consent["product_analytics"]:
                raise AccountError(403, "برای ثبت تحلیل رفتار باید رضایت فعال داشته باشید.")
            for event in clean:
                fingerprint = hashlib.sha256(
                    json.dumps(event, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                existing = con.execute(
                    """SELECT payload_sha256 FROM site_analytics_events
                       WHERE user_id=? AND client_event_id=?""",
                    (user["id"], event["client_event_id"]),
                ).fetchone()
                if existing:
                    if not hmac.compare_digest(existing["payload_sha256"], fingerprint):
                        raise AccountError(409, "شناسه رویداد قبلاً با محتوای دیگری استفاده شده است.")
                    duplicates += 1
                    continue
                con.execute(
                    """INSERT INTO site_analytics_events
                       (user_id,session_hash,client_event_id,payload_sha256,event_name,
                        page_path,component_id,element_role,scroll_depth_pct,
                        active_duration_ms,occurred_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        user["id"], session_hash, event["client_event_id"], fingerprint,
                        event["event_name"], event["page_path"], event["component_id"],
                        event["element_role"], event["scroll_depth_pct"],
                        event["active_duration_ms"], iso_now(),
                    ),
                )
                inserted += 1
            con.commit()
            return {"received": len(clean), "inserted": inserted, "duplicates": duplicates}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _analytics_path_report(
        self, *, days=30, max_steps=5, min_repetitions=2, limit=50,
        target_user_id=None,
    ):
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 730:
            raise AccountError(400, "بازه گزارش باید بین ۱ تا ۷۳۰ روز باشد.")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 2 <= max_steps <= 5:
            raise AccountError(400, "طول مسیر باید بین ۲ تا ۵ صفحه باشد.")
        if isinstance(min_repetitions, bool) or not isinstance(min_repetitions, int) or not 2 <= min_repetitions <= 10_000:
            raise AccountError(400, "حداقل تکرار مسیر معتبر نیست.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AccountError(400, "تعداد نتایج باید بین ۱ تا ۱۰۰ باشد.")

        cutoff = (utc_now() - timedelta(days=days)).isoformat(timespec="seconds")
        con = self._connect()
        try:
            users = con.execute(
                """SELECT COUNT(DISTINCT e.user_id)
                   FROM site_analytics_events e
                   JOIN site_analytics_consents c ON c.user_id=e.user_id
                   WHERE c.product_analytics=1 AND e.event_name='page_view'
                     AND e.occurred_at>=?""",
                (cutoff,),
            ).fetchone()[0]
            if target_user_id is None and users < 5:
                return {"suppressed": True, "minimum_cohort": 5, "paths": []}

            sql = (
                """SELECT e.id,e.user_id,e.session_hash,e.page_path,e.occurred_at
                   FROM site_analytics_events e
                   JOIN site_analytics_consents c ON c.user_id=e.user_id
                   WHERE c.product_analytics=1 AND e.event_name='page_view'
                     AND e.occurred_at>=?"""
            )
            params = [cutoff]
            if target_user_id is not None:
                sql += " AND e.user_id=?"
                params.append(int(target_user_id))
            sql += " ORDER BY e.user_id,e.session_hash,e.id"
            rows = con.execute(sql, params).fetchall()

            sessions = {}
            for row in rows:
                key = (row["user_id"], row["session_hash"])
                sessions.setdefault(key, []).append(row)

            grouped = {}
            for (owner_id, session_hash), items in sessions.items():
                for start in range(len(items)):
                    for length in range(2, min(max_steps, len(items) - start) + 1):
                        path = tuple(item["page_path"] for item in items[start:start + length])
                        entry = grouped.setdefault(path, {
                            "repetitions": 0, "sessions": set(), "users": set(),
                            "last_occurrence_at": None,
                        })
                        entry["repetitions"] += 1
                        entry["sessions"].add(session_hash)
                        entry["users"].add(owner_id)
                        at = items[start]["occurred_at"]
                        if entry["last_occurrence_at"] is None or at > entry["last_occurrence_at"]:
                            entry["last_occurrence_at"] = at

            result = []
            for path, entry in grouped.items():
                user_count = len(entry["users"])
                if entry["repetitions"] < min_repetitions:
                    continue
                if target_user_id is None and user_count < 5:
                    continue
                result.append({
                    "path": list(path),
                    "path_length": len(path),
                    "repetitions": entry["repetitions"],
                    "sessions": len(entry["sessions"]),
                    "users": user_count,
                    "last_occurrence_at": entry["last_occurrence_at"],
                })
            result.sort(
                key=lambda item: (
                    -item["repetitions"], -item["users"], -item["sessions"],
                    -item["path_length"], item["path"],
                )
            )
            return {
                "suppressed": False,
                "minimum_cohort": 5 if target_user_id is None else None,
                "paths": result[:limit],
            }
        finally:
            con.close()

    def analytics_overview(self, *, days=30, limit=20):
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 730:
            raise AccountError(400, "بازه گزارش باید بین ۱ تا ۷۳۰ روز باشد.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AccountError(400, "تعداد صفحات باید بین ۱ تا ۱۰۰ باشد.")
        cutoff = (utc_now() - timedelta(days=days)).isoformat(timespec="seconds")
        con = self._connect()
        try:
            active_users = con.execute(
                """SELECT COUNT(DISTINCT e.user_id)
                   FROM site_analytics_events e
                   JOIN site_analytics_consents c ON c.user_id=e.user_id
                   WHERE c.product_analytics=1 AND e.occurred_at>=?""",
                (cutoff,),
            ).fetchone()[0]
            if active_users < 5:
                return {"suppressed": True, "minimum_cohort": 5}
            row = con.execute(
                """SELECT COUNT(*) AS event_count,
                          COUNT(DISTINCT e.user_id) AS active_users,
                          COUNT(DISTINCT e.session_hash) AS sessions,
                          SUM(CASE WHEN e.event_name='page_view' THEN 1 ELSE 0 END) AS page_views,
                          SUM(CASE WHEN e.event_name='click' THEN 1 ELSE 0 END) AS clicks,
                          SUM(CASE WHEN e.event_name='scroll' THEN 1 ELSE 0 END) AS scroll_events,
                          SUM(CASE WHEN e.event_name='feature_view' THEN 1 ELSE 0 END) AS feature_views,
                          COALESCE(SUM(e.active_duration_ms),0) AS active_duration_ms
                   FROM site_analytics_events e
                   JOIN site_analytics_consents c ON c.user_id=e.user_id
                   WHERE c.product_analytics=1 AND e.occurred_at>=?""",
                (cutoff,),
            ).fetchone()
            top_pages = con.execute(
                """SELECT e.page_path,COUNT(*) AS views,
                          COUNT(DISTINCT e.user_id) AS users,
                          COUNT(DISTINCT e.session_hash) AS sessions
                   FROM site_analytics_events e
                   JOIN site_analytics_consents c ON c.user_id=e.user_id
                   WHERE c.product_analytics=1 AND e.event_name='page_view'
                     AND e.occurred_at>=?
                   GROUP BY e.page_path
                   HAVING COUNT(DISTINCT e.user_id)>=5
                   ORDER BY views DESC,e.page_path LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
            return {
                "suppressed": False,
                "days": days,
                "minimum_cohort": 5,
                "overview": dict(row),
                "top_pages": [dict(item) for item in top_pages],
            }
        finally:
            con.close()

    def purge_analytics_events(self, *, retention_days=365):
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 30 <= retention_days <= 730
        ):
            raise AccountError(400, "دوره نگهداری باید بین ۳۰ تا ۷۳۰ روز باشد.")
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        con = self._connect()
        try:
            with con:
                cur = con.execute(
                    "DELETE FROM site_analytics_events WHERE occurred_at<?",
                    (cutoff,),
                )
                return cur.rowcount
        finally:
            con.close()

    def analytics_user_events(self, *, target_user_id, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise AccountError(400, "تعداد رویدادها باید بین ۱ تا ۲۰۰ باشد.")
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT event_name,page_path,component_id,element_role,
                          scroll_depth_pct,active_duration_ms,occurred_at
                   FROM site_analytics_events
                   WHERE user_id=? ORDER BY id DESC LIMIT ?""",
                (int(target_user_id), limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            con.close()

    def audit_analytics_user_read(self, *, actor_user_id, target_user_id, action):
        con = self._connect()
        try:
            with con:
                con.execute(
                    """INSERT INTO site_analytics_audit
                       (actor_user_id,target_user_id,action,created_at)
                       VALUES (?,?,?,?)""",
                    (int(actor_user_id), int(target_user_id), action, iso_now()),
                )
        finally:
            con.close()

    @staticmethod
    def handles(path: str) -> bool:
        """Return whether a path belongs to this service (including callbacks)."""
        return (
            path == "/api/v1/public/plans"
            or path.startswith("/api/v1/auth/")
            or path.startswith("/api/v1/payments/")
            or path.startswith("/api/v1/subscriptions/")
            or path == "/api/v1/wallet"
            or path == "/api/v1/tickets"
            or path.startswith("/api/v1/tickets/")
            or path.startswith("/api/v1/site-admin/")
            or path in ("/api/v1/analytics/consent", "/api/v1/analytics/events",
                        "/api/v1/analytics/admin/overview", "/api/v1/analytics/admin/paths",
                        "/api/v1/analytics/admin/retention/expired")
            or re.fullmatch(r"/api/v1/analytics/admin/users/[0-9]+/(?:events|paths)", path)
            or path == "/api/v1/market/sector-config"
        )

    def dispatch(self, method: str, path: str, params: dict, body: dict,
                 user: dict | None, base_url: str):
        secure = base_url.lower().startswith("https://")
        if path == "/api/v1/public/plans" and method == "GET":
            return {"payload": self.public_plans()}
        if path == "/api/v1/auth/me" and method == "GET":
            return {"payload": {"user": user}}
        if path == "/api/v1/auth/register" and method == "POST":
            user_id = self.register(body)
            return self._session_result(user_id, bool(body.get("remember")), secure)
        if path == "/api/v1/auth/login" and method == "POST":
            user_id = self.login(body)
            return self._session_result(user_id, bool(body.get("remember")), secure)
        if path == "/api/v1/auth/otp/request" and method == "POST":
            return {"payload": self.request_otp(body)}
        if path == "/api/v1/auth/otp/verify" and method == "POST":
            user_id = self.verify_otp(body)
            return self._session_result(user_id, bool(body.get("remember", True)), secure)
        if path == "/api/v1/auth/google/start" and method == "GET":
            referral = (params.get("ref") or [""])[0]
            return {"status": 302,
                    "redirect": self.google_start(base_url, referral)}
        if path == "/api/v1/auth/google/callback" and method == "GET":
            user_id = self.google_callback(params)
            result = self._session_result(user_id, True, secure)
            result["status"] = 302
            result["redirect"] = "/?auth=success"
            return result
        if path == "/api/v1/payments/zarinpal/callback" and method == "GET":
            result = self.verify_zarinpal(params)
            result["success"] = bool(result.get("success"))
            return {"status": 302,
                    "redirect": "/?payment=" + ("success" if result["success"] else "failed")}
        if path == "/api/v1/auth/logout" and method == "POST":
            if user:
                self.check_csrf(user, body.get("_csrf") or body.get("csrf_token"))
            if user and user.get("session_hash"):
                con = self._connect()
                try:
                    with con:
                        con.execute("DELETE FROM site_sessions WHERE session_hash=?",
                                    (user["session_hash"],))
                finally:
                    con.close()
            return {"payload": {"logged_out": True},
                    "headers": {"Set-Cookie": self.clear_session_cookie(secure)}}
        self.require_user(user)
        if path == "/api/v1/analytics/consent" and method == "GET":
            return {"payload": {"consent": self.analytics_consent(user)}}
        if path == "/api/v1/analytics/consent" and method == "PUT":
            self.check_csrf(user, body.get("_csrf") or body.get("csrf_token"))
            consent = self.set_analytics_consent(user, body.get("product_analytics"))
            return {"payload": {"consent": consent}}
        if path == "/api/v1/analytics/events" and method == "POST":
            self.check_csrf(user, body.get("_csrf") or body.get("csrf_token"))
            return {"payload": self.record_analytics_events(user, body.get("events"))}
        if path == "/api/v1/analytics/admin/overview" and method == "GET":
            self.require_role(user, ("admin", "super_admin"))
            def query_int(name, default):
                raw = (params.get(name) or [str(default)])[0]
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    raise AccountError(400, "پارامتر گزارش معتبر نیست.")
            return {"payload": self.analytics_overview(
                days=query_int("days", 30), limit=query_int("limit", 20)
            )}
        if path == "/api/v1/analytics/admin/paths" and method == "GET":
            self.require_role(user, ("admin", "super_admin"))
            def query_int(name, default):
                raw = (params.get(name) or [str(default)])[0]
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    raise AccountError(400, "پارامتر گزارش معتبر نیست.")
            report = self._analytics_path_report(
                days=query_int("days", 30),
                max_steps=query_int("max_steps", 5),
                min_repetitions=query_int("min_repetitions", 2),
                limit=query_int("limit", 50),
            )
            return {"payload": report}
        analytics_user_match = re.fullmatch(
            r"/api/v1/analytics/admin/users/([0-9]+)/(events|paths)", path
        )
        if path == "/api/v1/analytics/admin/retention/expired" and method == "DELETE":
            self.require_role(user, ("super_admin",))
            self.check_csrf(user, body.get("_csrf") or body.get("csrf_token"))
            raw_days = (params.get("days") or ["365"])[0]
            try:
                retention_days = int(raw_days)
            except (TypeError, ValueError):
                raise AccountError(400, "دوره نگهداری معتبر نیست.")
            return {"payload": {
                "deleted_events": self.purge_analytics_events(retention_days=retention_days),
                "retention_days": retention_days,
            }}
        if analytics_user_match and method == "GET":
            self.require_role(user, ("super_admin",))
            target_user_id = int(analytics_user_match.group(1))
            kind = analytics_user_match.group(2)
            con = self._connect()
            try:
                exists = con.execute(
                    "SELECT 1 FROM site_users WHERE id=? AND is_active=1",
                    (target_user_id,),
                ).fetchone()
                if not exists:
                    raise AccountError(404, "کاربر موردنظر پیدا نشد.")
            finally:
                con.close()
            def query_int(name, default):
                raw = (params.get(name) or [str(default)])[0]
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    raise AccountError(400, "پارامتر گزارش معتبر نیست.")
            self.audit_analytics_user_read(
                actor_user_id=user["id"], target_user_id=target_user_id,
                action="user_paths.read" if kind == "paths" else "user_events.read",
            )
            if kind == "paths":
                report = self._analytics_path_report(
                    days=query_int("days", 30),
                    max_steps=query_int("max_steps", 5),
                    min_repetitions=query_int("min_repetitions", 2),
                    limit=query_int("limit", 50),
                    target_user_id=target_user_id,
                )
                report["user_id"] = target_user_id
                return {"payload": report}
            return {"payload": {
                "user_id": target_user_id,
                "events": self.analytics_user_events(
                    target_user_id=target_user_id, limit=query_int("limit", 100)
                ),
            }}
        if user.get("must_change_password") and path not in (
                "/api/v1/auth/change-password", "/api/v1/auth/logout",
                "/api/v1/auth/me"):
            raise AccountError(403, "برای ادامه کار ابتدا رمز عبور موقت را تغییر دهید.")
        if method in ("POST", "PUT", "DELETE"):
            self.check_csrf(user, body.get("_csrf") or body.get("csrf_token") or
                             (body.get("headers", {}) or {}).get("X-CSRF-Token"))
        if path == "/api/v1/auth/change-password" and method == "POST":
            password = self._validate_password(body.get("new_password"))
            con = self._connect()
            try:
                with con:
                    con.execute("UPDATE site_users SET password_hash=?,must_change_password=0 "
                                "WHERE id=?",
                                (self._password_hash(password), user["id"]))
                    con.execute("DELETE FROM site_sessions WHERE user_id=? AND session_hash<>?",
                                (user["id"], user.get("session_hash", "")))
            finally:
                con.close()
            return {"payload": {"changed": True}}
        if path == "/api/v1/auth/bootstrap-superadmin" and method == "POST":
            return {"payload": self.bootstrap_superadmin(user, body)}
        if path == "/api/v1/wallet" and method == "GET":
            return {"payload": self.wallet(user["id"])}
        if path == "/api/v1/subscriptions/plans" and method == "GET":
            return {"payload": self.public_plans()}
        if path == "/api/v1/subscriptions/checkout" and method == "POST":
            return {"payload": self.create_checkout(user, body, base_url)}
        if path == "/api/v1/tickets" and method == "GET":
            return {"payload": self.list_tickets(user)}
        if path == "/api/v1/tickets" and method == "POST":
            return {"payload": self.create_ticket(user["id"], body), "status": 201}
        parts = [p for p in path.split("/") if p]
        if len(parts) == 5 and parts[:3] == ["api", "v1", "tickets"]:
            ticket_id = int(parts[3])
            action = parts[4]
            if action == "messages" and method == "GET":
                return {"payload": self.ticket_messages(user, ticket_id)}
            if action == "messages" and method == "POST":
                return {"payload": self.ticket_reply(user, ticket_id, body)}
            if action in ("status", "assign") and method == "POST":
                self.require_role(user, ("support", "admin", "super_admin"))
                update_body = dict(body)
                if action == "status":
                    update_body.pop("assigned_to", None)
                else:
                    update_body.pop("status", None)
                return {"payload": self.ticket_update(user, ticket_id, update_body)}
        if path == "/api/v1/site-admin/overview" and method == "GET":
            self.require_role(user, ("super_admin", "admin"))
            con = self._connect()
            try:
                stats = {
                    "users": con.execute("SELECT COUNT(*) FROM site_users").fetchone()[0],
                    "premium_users": con.execute(
                        "SELECT COUNT(DISTINCT user_id) FROM site_subscriptions "
                        "WHERE status='active' AND ends_at>?", (iso_now(),)).fetchone()[0],
                    "open_tickets": con.execute(
                        "SELECT COUNT(*) FROM site_tickets WHERE status<>'closed'").fetchone()[0],
                    "paid_orders": con.execute(
                        "SELECT COUNT(*) FROM site_payments WHERE status='paid'").fetchone()[0],
                }
                return {"payload": stats}
            finally:
                con.close()
        if path == "/api/v1/site-admin/staff" and method == "GET":
            self.require_role(user, ("super_admin", "admin"))
            return {"payload": self.staff_list()}
        if path == "/api/v1/site-admin/staff" and method == "POST":
            self.require_role(user, ("super_admin",))
            return {"payload": self.create_staff(body), "status": 201}
        if path == "/api/v1/site-admin/users" and method == "GET":
            self.require_role(user, ("super_admin", "admin"))
            return {"payload": self.users_list((params.get("q") or [""])[0])}
        if len(parts) == 5 and parts[:3] == ["api", "v1", "site-admin"] and parts[3] == "staff" and method == "POST" and parts[4].isdigit():
            self.require_role(user, ("super_admin",))
            return {"payload": self.change_staff_role(int(parts[4]), body)}
        if path == "/api/v1/market/sector-config" and method == "GET":
            return {"payload": self.get_sector_mapping(user["id"])}
        if path == "/api/v1/market/sector-config" and method in ("PUT", "POST"):
            return {"payload": self.save_sector_mapping(user, body)}
        raise AccountError(404, "مسیر حساب کاربری پیدا نشد.")

    def public_plans(self):
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT slug,name,duration_days,price_toman FROM site_plans "
                "WHERE is_active=1 ORDER BY duration_days").fetchall()
            return {
                "currency": "TOMAN",
                "referral_reward_toman": REFERRAL_REWARD_TOMAN,
                "promotions": [{"code": "WELCOME15", "discount_percent": 15,
                                "new_customer_only": True}],
                "plans": [dict(r) for r in rows],
                "features": {
                    "free": ["نمای کلی بازار", "نمودارهای پایه", "دیده‌بان محدود"],
                    "premium": ["تحلیل گرایش صنایع و پرتفوی", "سفارشی‌سازی گروه‌بندی بازار",
                                "اندیکاتورها و ابزارهای پیشرفته نمودار", "فیلترهای تحلیلی",
                                "پشتیبانی با اولویت"],
                },
            }
        finally:
            con.close()
