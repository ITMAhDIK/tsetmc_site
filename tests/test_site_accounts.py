import json
import sqlite3
from datetime import timedelta

import pytest

from site_accounts import AccountError, AccountService, iso_now, utc_now


@pytest.fixture
def service(tmp_path):
    return AccountService(str(tmp_path / "site_accounts.sqlite"))


def active_user(service, user_id):
    con = service._connect()
    try:
        return service._safe_user(con, user_id)
    finally:
        con.close()


def test_password_login_and_persistent_cookie_session(service):
    user_id = service.register({
        "username": "analyst_01",
        "password": "a-secure-password-2026",
        "display_name": "Analyst",
    })

    assert service.login({
        "identifier": "analyst_01",
        "password": "a-secure-password-2026",
    }) == user_id

    raw, payload, ttl = service._new_session(user_id, remember=True)
    authenticated = service.authenticate(raw)

    assert ttl == 30 * 24 * 3600
    assert payload["username"] == "analyst_01"
    assert authenticated["id"] == user_id
    assert authenticated["csrf_token"]
    assert service._token_hash(raw) != raw
    assert "HttpOnly" in service.session_cookie(raw, ttl, secure=True)
    assert "Secure" in service.session_cookie(raw, ttl, secure=True)


def test_login_rejects_wrong_password(service):
    service.register({
        "username": "analyst_02",
        "password": "another-secure-password",
    })
    with pytest.raises(AccountError) as exc:
        service.login({"identifier": "analyst_02", "password": "wrong"})
    assert exc.value.status == 401


def test_phone_otp_creates_account_and_limited_trial(service):
    phone = "+989121234567"
    code = "123456"
    con = service._connect()
    try:
        with con:
            now = utc_now()
            con.execute(
                "INSERT INTO site_otp_challenges(phone,code_hash,referral_code,"
                "attempts,expires_at,created_at) VALUES (?,?,NULL,0,?,?)",
                (phone, service._token_hash(code),
                 (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds")),
            )
    finally:
        con.close()

    user_id = service.verify_otp({"phone": phone, "code": code})
    user = active_user(service, user_id)
    assert user["phone"] == phone
    assert user["phone_verified"] == 1
    assert user["is_premium"] is True
    assert user["subscription"]["plan_slug"] == "trial_3"


def test_referrer_receives_exactly_one_reward_after_first_paid_subscription(service):
    inviter_id = service.register({
        "username": "referrer_01",
        "password": "referrer-password-123",
    })
    inviter = active_user(service, inviter_id)
    invitee_id = service.register({
        "username": "invitee_01",
        "password": "invitee-password-123",
        "referral_code": inviter["referral_code"],
    })

    con = service._connect()
    try:
        with con:
            cursor = con.execute(
                "INSERT INTO site_payments(order_id,user_id,plan_slug,list_price_toman,"
                "discount_toman,wallet_used_toman,amount_toman,amount_rial,gateway,"
                "status,created_at) VALUES ('test-order-01',?,'pro_30',690000,0,0,"
                "690000,6900000,'test','pending',?)",
                (invitee_id, iso_now()),
            )
            payment_id = cursor.lastrowid
            service._settle_payment(con, payment_id, "paid", "test-ref")
            # A duplicate callback must not generate another referral reward.
            service._settle_payment(con, payment_id, "paid", "test-ref")
    finally:
        con.close()

    assert service.wallet(inviter_id)["balance_toman"] == 100_000
    assert service.wallet(invitee_id)["balance_toman"] == 0


def test_ticket_access_and_support_staff_assignment(service):
    customer_id = service.register({
        "username": "customer_01",
        "password": "customer-password-123",
    })
    customer = active_user(service, customer_id)
    ticket = service.create_ticket(customer_id, {
        "subject": "پرسش درباره اشتراک",
        "message": "لطفاً وضعیت اشتراک من را بررسی کنید.",
    })
    ticket_id = ticket["id"]

    staff = service.create_staff({
        "username": "support_01",
        "temporary_password": "support-temp-password-123",
        "display_name": "Support Agent",
        "role": "support",
    })
    staff_user_id = None
    con = service._connect()
    try:
        row = con.execute(
            "SELECT id FROM site_users WHERE username='support_01'"
        ).fetchone()
        staff_user_id = row["id"]
    finally:
        con.close()

    support = active_user(service, staff_user_id)
    service.ticket_update(support, ticket_id, {"assigned_to": str(staff_user_id)})
    service.ticket_reply(support, ticket_id, {"message": "تیکت دریافت شد."})

    assert service.list_tickets(customer)["tickets"][0]["id"] == ticket_id
    assert service.list_tickets(support)["tickets"][0]["assigned_to"] == staff_user_id
    assert staff["staff"]["role"] == "support"


def test_only_premium_users_can_customize_sector_mapping(service):
    user_id = service.register({
        "username": "free_user_01",
        "password": "free-user-password-123",
    })
    user = active_user(service, user_id)

    with pytest.raises(AccountError) as exc:
        service.save_sector_mapping(user, {"12": "بانک‌ها"})
    assert exc.value.status == 402

    premium = dict(user, is_premium=True)
    result = service.save_sector_mapping(premium, {"mapping": {
        "12": "بانک‌ها",
        "13": "سرمایه‌گذاری",
    }})
    assert result["saved"] is True
    assert service.get_sector_mapping(user_id)["mapping"] == {
        "12": "بانک‌ها",
        "13": "سرمایه‌گذاری",
    }


def test_superadmin_is_the_only_role_that_can_create_staff(service):
    normal_user = service.register({
        "username": "ordinary_user_01",
        "password": "ordinary-password-123",
    })
    normal = active_user(service, normal_user)

    with pytest.raises(AccountError) as exc:
        service.dispatch(
            "POST", "/api/v1/site-admin/staff", {}, {
                "username": "new_admin_01",
                "temporary_password": "admin-temp-password-123",
                "role": "admin",
            }, normal, "http://localhost:8000",
        )
    assert exc.value.status == 403


def test_account_route_matcher_is_scoped():
    assert AccountService.handles("/api/v1/auth/login")
    assert AccountService.handles("/api/v1/tickets")
    assert AccountService.handles("/api/v1/market/sector-config")
    assert not AccountService.handles("/api/v1/market/overview")
    assert not AccountService.handles("/api/v1/stocks/ABC/prices")


def test_phone_login_accepts_local_09xx_format(service):
    user_id = service.register({
        "username": "phone_login_01",
        "password": "phone-login-password-123",
        "phone": "09121234567",
    })
    assert service.login({
        "identifier": "09121234567",
        "password": "phone-login-password-123",
    }) == user_id
    assert service.login({
        "identifier": "+989121234567",
        "password": "phone-login-password-123",
    }) == user_id


def test_google_oauth_state_keeps_referral_code(monkeypatch, service):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.test/api/v1/auth/google/callback")
    service.google_start("https://example.test", "REFCODE1")
    con = service._connect()
    try:
        row = con.execute(
            "SELECT referral_code FROM site_oauth_states ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row["referral_code"] == "REFCODE1"
    finally:
        con.close()


def test_temporary_staff_password_must_be_changed_before_private_endpoints(service):
    staff = service.create_staff({
        "username": "temp_staff_01",
        "temporary_password": "temporary-password-123",
        "role": "support",
    })
    con = service._connect()
    try:
        row = con.execute("SELECT id FROM site_users WHERE username=?",
                          ("temp_staff_01",)).fetchone()
        user = service._safe_user(con, row["id"], csrf_token="test-csrf")
    finally:
        con.close()

    with pytest.raises(AccountError) as exc:
        service.dispatch("GET", "/api/v1/wallet", {}, {}, user, "http://localhost")
    assert exc.value.status == 403

    service.dispatch("POST", "/api/v1/auth/change-password", {}, {
        "new_password": "new-temporary-password-456",
        "_csrf": "test-csrf",
    }, user, "http://localhost")
    updated = active_user(service, user["id"])
    assert updated["must_change_password"] == 0
    assert service.login({
        "identifier": "temp_staff_01",
        "password": "new-temporary-password-456",
    }) == user["id"]
