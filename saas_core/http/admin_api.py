"""Authenticated WSGI API for the SaaS staff console.

Authentication is based on the hashed server-side session record. The browser
cannot supply actor IDs, roles or permissions. Every mutation requires an
allow-listed Origin plus a session-bound double-submit CSRF token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from urllib.parse import parse_qs
from collections.abc import Mapping

from saas_core.domain.admin_console import AdminConsoleError, AdminConsoleService
from saas_core.domain.product_analytics import ProductAnalyticsError, ProductAnalyticsService

API_PREFIX = "/api/v1/admin"
SESSION_COOKIE = "__Host-tloomberg_session"
CSRF_COOKIE = "__Host-tloomberg_csrf"
MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_FIELDS = 30
STAFF_PERMISSIONS = frozenset({
    "admin.console.read", "users.read", "users.manage", "roles.manage",
    "subscriptions.manage", "payments.read", "payments.reconcile",
    "wallet.read", "wallet.adjust", "withdrawals.review", "referrals.read",
    "tickets.manage", "faq.manage", "analytics.read", "audit.read", "market.admin",
})


class AdminApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _row_value(row, key, index=0):
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _json_default(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError("Value is not JSON serializable: %s" % type(value).__name__)


def _safe_json(payload):
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
        default=_json_default, allow_nan=False,
    ).encode("utf-8")


def _hash_token(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SaasAdminAPI:
    """Dedicated SaaS-only admin API, compatible with WSGI servers."""

    def __init__(
        self,
        *,
        connection_factory,
        allowed_origins,
        analytics_service=None,
        published_consent_version=None,
        max_body_bytes=MAX_BODY_BYTES,
    ):
        if not callable(connection_factory):
            raise ValueError("connection_factory must be callable.")
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise ValueError("max_body_bytes must be an integer.")
        if not 1024 <= max_body_bytes <= 1024 * 1024:
            raise ValueError("max_body_bytes must be between 1 KiB and 1 MiB.")
        origins = set()
        for origin in allowed_origins or ():
            if not isinstance(origin, str) or not re.fullmatch(r"https?://[A-Za-z0-9.:-]+", origin):
                raise ValueError("Every allowed origin must be a bare HTTP(S) origin.")
            origins.add(origin.rstrip("/"))
        if not origins:
            raise ValueError("At least one explicit browser origin is required.")

        self._connection_factory = connection_factory
        self._allowed_origins = frozenset(origins)
        self._max_body_bytes = max_body_bytes
        self.admin = AdminConsoleService(
            connection_factory=connection_factory,
            permission_checker=self._has_permission,
        )
        if analytics_service is not None:
            self.analytics = analytics_service
        elif published_consent_version:
            self.analytics = ProductAnalyticsService(
                connection_factory=connection_factory,
                permission_checker=self._has_permission,
                published_consent_version=published_consent_version,
            )
        else:
            self.analytics = None

    def _has_permission(self, actor_user_id, permission_code):
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN role_permissions rp ON rp.role_id=ur.role_id
                    JOIN permissions p ON p.id=rp.permission_id
                    WHERE u.id=%s AND u.status='active' AND p.code=%s
                )
                """,
                (actor_user_id, permission_code),
            )
            result = cursor.fetchone()
            return bool(_row_value(result, "exists", 0)) if result is not None else False
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def _has_staff_permission(self, actor_user_id):
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN role_permissions rp ON rp.role_id=ur.role_id
                    JOIN permissions p ON p.id=rp.permission_id
                    WHERE u.id=%s AND u.status='active' AND p.code = ANY(%s)
                )
                """,
                (actor_user_id, list(STAFF_PERMISSIONS)),
            )
            row = cursor.fetchone()
            return bool(_row_value(row, "exists", 0)) if row is not None else False
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def _authenticate(self, environ):
        cookie = SimpleCookie()
        try:
            cookie.load(environ.get("HTTP_COOKIE", ""))
        except Exception:
            raise AdminApiError(400, "invalid_cookie", "Cookie header is malformed.") from None
        cookies = {key: morsel.value for key, morsel in cookie.items()}
        token = cookies.get(SESSION_COOKIE, "")
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise AdminApiError(401, "authentication_required", "A valid staff session is required.")
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT s.id AS session_id,s.user_id,s.csrf_token_hash,
                       s.mfa_verified_at,u.status
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.revoked_at IS NULL
                  AND s.expires_at>now()
                """,
                (_hash_token(token),),
            )
            row = cursor.fetchone()
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()
        if row is None:
            raise AdminApiError(401, "authentication_required", "A valid staff session is required.")
        principal = {
            "session_id": _row_value(row, "session_id", 0),
            "actor_user_id": _row_value(row, "user_id", 1),
            "csrf_token_hash": _row_value(row, "csrf_token_hash", 2),
            "mfa_verified_at": _row_value(row, "mfa_verified_at", 3),
            "user_status": _row_value(row, "status", 4),
            "cookies": cookies,
        }
        if principal["user_status"] != "active":
            raise AdminApiError(401, "account_inactive", "The account is not active.")
        if principal["mfa_verified_at"] is None:
            raise AdminApiError(403, "mfa_required", "A staff MFA-verified session is required.")
        return principal

    def _require_origin(self, environ, *, mutation):
        origin = environ.get("HTTP_ORIGIN")
        if mutation and not origin:
            raise AdminApiError(403, "origin_required", "Origin is required for a state-changing request.")
        if origin and origin.rstrip("/") not in self._allowed_origins:
            raise AdminApiError(403, "origin_denied", "This browser origin is not allowed.")
        return origin.rstrip("/") if origin else None

    @staticmethod
    def _require_csrf(principal, environ):
        cookie_token = principal["cookies"].get(CSRF_COOKIE, "")
        header_token = environ.get("HTTP_X_CSRF_TOKEN", "")
        stored_hash = principal["csrf_token_hash"]
        if (
            not cookie_token or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
            or not isinstance(stored_hash, str)
            or not hmac.compare_digest(_hash_token(header_token), stored_hash.strip())
        ):
            raise AdminApiError(403, "csrf_failed", "The CSRF token is missing or invalid.")

    def _read_body(self, environ):
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except (TypeError, ValueError):
            raise AdminApiError(400, "invalid_content_length", "Content-Length is invalid.") from None
        if length < 0 or length > self._max_body_bytes:
            raise AdminApiError(413, "body_too_large", "The request body exceeds the allowed size.")
        if length == 0:
            return {}
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise AdminApiError(415, "json_required", "Content-Type must be application/json.")
        raw = environ["wsgi.input"].read(length)
        if len(raw) != length:
            raise AdminApiError(400, "incomplete_body", "The request body is incomplete.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AdminApiError(400, "invalid_json", "The request body must be valid UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise AdminApiError(400, "json_object_required", "The request body must be a JSON object.")
        return payload

    @staticmethod
    def _query(environ):
        try:
            return parse_qs(
                environ.get("QUERY_STRING", ""), keep_blank_values=True,
                strict_parsing=False, max_num_fields=MAX_QUERY_FIELDS,
            )
        except ValueError:
            raise AdminApiError(400, "invalid_query", "The query string is malformed.") from None

    @staticmethod
    def _one(query, key, default=None):
        values = query.get(key)
        if not values:
            return default
        if len(values) != 1:
            raise AdminApiError(400, "duplicate_query_parameter", "%s may appear only once." % key)
        return values[0]

    @staticmethod
    def _required(mapping, key):
        if key not in mapping:
            raise AdminApiError(400, "missing_field", "%s is required." % key)
        return mapping[key]

    @staticmethod
    def _request_id(environ):
        value = environ.get("HTTP_X_REQUEST_ID")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
            return value
        return uuid.uuid4().hex

    def _analytics_required(self):
        if self.analytics is None:
            raise AdminApiError(503, "analytics_unavailable", "The analytics reporting service is not configured.")
        return self.analytics

    def _dispatch(self, method, path, query, body, principal, environ):
        actor = principal["actor_user_id"]
        request_id = self._request_id(environ)

        if method == "GET" and path == API_PREFIX + "/me":
            if not self._has_staff_permission(actor):
                raise AdminApiError(403, "permission_denied", "Staff permission is required.")
            return self.admin.admin_context(actor_user_id=actor)

        if method == "GET" and path == API_PREFIX + "/overview":
            return self.admin.overview(actor_user_id=actor)
        if method == "GET" and path == API_PREFIX + "/users":
            return self.admin.list_users(
                actor_user_id=actor, query=self._one(query, "q"),
                status=self._one(query, "status"),
                limit=self._one(query, "limit", "50"),
                offset=self._one(query, "offset", "0"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/users/([0-9a-fA-F-]{36})", path)
        if method == "GET" and match:
            return self.admin.user_detail(actor_user_id=actor, target_user_id=match.group(1))
        match = re.fullmatch(re.escape(API_PREFIX) + r"/users/([0-9a-fA-F-]{36})/status", path)
        if method == "PATCH" and match:
            return self.admin.set_user_status(
                actor_user_id=actor, target_user_id=match.group(1),
                status=self._required(body, "status"), request_id=request_id,
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/users/([0-9a-fA-F-]{36})/roles", path)
        if method == "POST" and match:
            return self.admin.assign_role(
                actor_user_id=actor, target_user_id=match.group(1),
                role_code=self._required(body, "role_code"), request_id=request_id,
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/users/([0-9a-fA-F-]{36})/roles/([a-z_]{2,32})", path)
        if method == "DELETE" and match:
            return self.admin.revoke_role(
                actor_user_id=actor, target_user_id=match.group(1),
                role_code=match.group(2), request_id=request_id,
            )
        if method == "GET" and path == API_PREFIX + "/subscriptions":
            return self.admin.list_subscriptions(
                actor_user_id=actor, status=self._one(query, "status"),
                limit=self._one(query, "limit", "50"), offset=self._one(query, "offset", "0"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/subscriptions/([0-9a-fA-F-]{36})/cancel", path)
        if method == "POST" and match:
            return self.admin.cancel_subscription(
                actor_user_id=actor, subscription_id=match.group(1), request_id=request_id,
            )
        if method == "GET" and path == API_PREFIX + "/payments/reconciliation":
            return self.admin.payment_reconciliation_queue(
                actor_user_id=actor, limit=self._one(query, "limit", "50"),
                offset=self._one(query, "offset", "0"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/payments/([0-9a-fA-F-]{36})/review", path)
        if method == "POST" and match:
            return self.admin.review_payment(
                actor_user_id=actor, order_id=match.group(1),
                outcome=self._required(body, "outcome"), note=body.get("note", ""),
                request_id=request_id,
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/users/([0-9a-fA-F-]{36})/wallet", path)
        if method == "GET" and match:
            return self.admin.wallet_summary(
                actor_user_id=actor, target_user_id=match.group(1),
                limit=self._one(query, "limit", "50"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/users/([0-9a-fA-F-]{36})/wallet/adjustments", path)
        if method == "POST" and match:
            return self.admin.adjust_wallet(
                actor_user_id=actor, target_user_id=match.group(1),
                bucket=self._required(body, "bucket"),
                direction=self._required(body, "direction"),
                amount_toman=self._required(body, "amount_toman"),
                idempotency_key=self._required(body, "idempotency_key"),
                reason=self._required(body, "reason"), request_id=request_id,
            )
        if method == "GET" and path == API_PREFIX + "/withdrawals":
            return self.admin.list_withdrawals(
                actor_user_id=actor, status=self._one(query, "status"),
                limit=self._one(query, "limit", "50"), offset=self._one(query, "offset", "0"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/withdrawals/([0-9a-fA-F-]{36})/review", path)
        if method == "POST" and match:
            return self.admin.review_withdrawal(
                actor_user_id=actor, withdrawal_id=match.group(1),
                decision=self._required(body, "decision"), request_id=request_id,
                note=body.get("note"), payout_reference=body.get("payout_reference"),
            )
        if method == "GET" and path == API_PREFIX + "/referrals":
            return self.admin.list_referrals(
                actor_user_id=actor, limit=self._one(query, "limit", "50"),
                offset=self._one(query, "offset", "0"),
            )
        if method == "GET" and path == API_PREFIX + "/tickets":
            return self.admin.list_tickets(
                actor_user_id=actor, status=self._one(query, "status"),
                limit=self._one(query, "limit", "50"), offset=self._one(query, "offset", "0"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/tickets/([0-9a-fA-F-]{36})", path)
        if method == "GET" and match:
            return self.admin.ticket_detail(actor_user_id=actor, ticket_id=match.group(1))
        if method == "PATCH" and match:
            return self.admin.update_ticket(
                actor_user_id=actor, ticket_id=match.group(1),
                changes=body, request_id=request_id,
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/tickets/([0-9a-fA-F-]{36})/internal-notes", path)
        if method == "POST" and match:
            return self.admin.add_internal_ticket_note(
                actor_user_id=actor, ticket_id=match.group(1),
                body=self._required(body, "body"), request_id=request_id,
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/tickets/([0-9a-fA-F-]{36})/replies", path)
        if method == "POST" and match:
            return self.admin.add_ticket_reply(
                actor_user_id=actor, ticket_id=match.group(1),
                body=self._required(body, "body"), request_id=request_id,
            )
        if method == "GET" and path == API_PREFIX + "/faq":
            published_value = self._one(query, "published")
            if published_value is not None and published_value.lower() not in {"true", "false"}:
                raise AdminApiError(400, "invalid_boolean", "published must be true or false.")
            published = None if published_value is None else published_value.lower() == "true"
            return self.admin.list_faq(
                actor_user_id=actor, published=published,
                limit=self._one(query, "limit", "100"), offset=self._one(query, "offset", "0"),
            )
        match = re.fullmatch(re.escape(API_PREFIX) + r"/faq/([a-z0-9]+(?:-[a-z0-9]+)*)", path)
        if method == "PUT" and match:
            return self.admin.upsert_faq(
                actor_user_id=actor, slug=match.group(1),
                question=self._required(body, "question"), answer=self._required(body, "answer"),
                published=self._required(body, "published"),
                sort_order=body.get("sort_order", 0), request_id=request_id,
            )
        if method == "DELETE" and match:
            return self.admin.archive_faq(actor_user_id=actor, slug=match.group(1), request_id=request_id)
        if method == "GET" and path == API_PREFIX + "/audit-logs":
            return self.admin.audit_logs(
                actor_user_id=actor, action=self._one(query, "action"),
                target_id=self._one(query, "target_id"),
                limit=self._one(query, "limit", "100"), offset=self._one(query, "offset", "0"),
            )

        analytics_path = API_PREFIX + "/analytics"
        if method == "GET" and path == analytics_path + "/components":
            return self._analytics_required().component_heatmap(
                actor_user_id=actor, start_at=self._one(query, "start_at"),
                end_at=self._one(query, "end_at"), page_path=self._one(query, "page_path"),
                limit=self._one(query, "limit", "200"),
            )
        if method == "GET" and path == analytics_path + "/funnel":
            raw_steps = self._one(query, "steps", "")
            steps = [part.strip() for part in raw_steps.split(",") if part.strip()]
            return self._analytics_required().funnel_report(
                actor_user_id=actor, steps=steps,
                start_at=self._one(query, "start_at"), end_at=self._one(query, "end_at"),
            )
        if method == "GET" and path == analytics_path + "/cohorts":
            return self._analytics_required().cohort_retention(
                actor_user_id=actor, start_at=self._one(query, "start_at"),
                end_at=self._one(query, "end_at"),
                activity_event=self._one(query, "activity_event", "page_view"),
                max_weeks=self._one(query, "max_weeks", "12"),
            )
        match = re.fullmatch(re.escape(analytics_path) + r"/users/([0-9a-fA-F-]{36})/events", path)
        if method == "GET" and match:
            return self._analytics_required().user_events(
                actor_user_id=actor, target_user_id=match.group(1), request_id=request_id,
                start_at=self._one(query, "start_at"), end_at=self._one(query, "end_at"),
                limit=self._one(query, "limit", "200"),
            )
        if method == "POST" and path == analytics_path + "/retention/purge":
            return self._analytics_required().purge_expired_events(
                actor_user_id=actor, request_id=request_id,
            )
        raise AdminApiError(404, "route_not_found", "The requested admin route does not exist.")

    def _response(self, start_response, status, payload, *, origin=None, headers=None):
        body = _safe_json(payload)
        response_headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store, private"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Vary", "Origin"),
        ]
        if origin:
            response_headers.extend([
                ("Access-Control-Allow-Origin", origin),
                ("Access-Control-Allow-Credentials", "true"),
            ])
        if headers:
            response_headers.extend(headers)
        phrases = {
            200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type",
            500: "Internal Server Error", 503: "Service Unavailable",
        }
        start_response("%d %s" % (status, phrases.get(status, "Error")), response_headers)
        return [body]

    def __call__(self, environ, start_response):
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = environ.get("PATH_INFO", "")
        origin = None
        try:
            origin = self._require_origin(environ, mutation=method not in {"GET", "HEAD", "OPTIONS"})
            if not path.startswith(API_PREFIX + "/") and path != API_PREFIX:
                raise AdminApiError(404, "route_not_found", "The requested route does not exist.")
            if method == "OPTIONS":
                body = b""
                headers = [
                    ("Content-Length", "0"),
                    ("Cache-Control", "no-store"),
                    ("Vary", "Origin"),
                    ("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS"),
                    ("Access-Control-Allow-Headers", "Content-Type,X-CSRF-Token,X-Request-ID"),
                    ("Access-Control-Max-Age", "600"),
                ]
                if origin:
                    headers.extend([
                        ("Access-Control-Allow-Origin", origin),
                        ("Access-Control-Allow-Credentials", "true"),
                    ])
                start_response("204 No Content", headers)
                return [body]
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                return self._response(
                    start_response, 405,
                    {"ok": False, "error": {"code": "method_not_allowed", "message": "HTTP method is not supported."}},
                    origin=origin, headers=[("Allow", "GET,POST,PUT,PATCH,DELETE,OPTIONS")],
                )
            principal = self._authenticate(environ)
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                self._require_csrf(principal, environ)
            query = self._query(environ)
            body = self._read_body(environ) if method in {"POST", "PUT", "PATCH"} else {}
            payload = self._dispatch(method, path, query, body, principal, environ)
            request_id = self._request_id(environ)
            return self._response(
                start_response, 200,
                {"ok": True, "data": payload, "request_id": request_id},
                origin=origin, headers=[("X-Request-ID", request_id)],
            )
        except AdminApiError as exc:
            return self._response(
                start_response, exc.status,
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                origin=origin,
            )
        except AdminConsoleError as exc:
            status = int(getattr(exc, "status", 400))
            code = {
                400: "invalid_request", 403: "permission_denied",
                404: "not_found", 409: "conflict", 500: "internal_error",
            }.get(status, "admin_error")
            return self._response(
                start_response, status,
                {"ok": False, "error": {"code": code, "message": str(exc)}},
                origin=origin,
            )
        except ProductAnalyticsError as exc:
            return self._response(
                start_response, int(getattr(exc, "status", 400)),
                {"ok": False, "error": {"code": "analytics_error", "message": str(exc)}},
                origin=origin,
            )
        except Exception:
            # Never leak SQL, connection strings, session tokens or stack traces.
            return self._response(
                start_response, 500,
                {"ok": False, "error": {"code": "internal_error", "message": "The admin request could not be completed."}},
                origin=origin,
            )


def create_admin_api(**kwargs):
    """Factory: deployment code must explicitly provide the SaaS DB and origins."""
    return SaasAdminAPI(**kwargs)
