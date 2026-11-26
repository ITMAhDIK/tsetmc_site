"""PostgreSQL integration tests for Phase 5 wallet and referral lifecycle."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from saas_core.wallet.wallet_repository import (
    get_wallet_balances, post_ledger_entry, request_withdrawal,
    review_withdrawal, settle_withdrawal, spend_wallet,
)
from saas_core.wallet.referrals import ensure_referral_code, attach_referral
from saas_core.payments.repository import create_subscription_order, create_payment_attempt, attach_provider_checkout
from saas_core.payments.domain import VerifiedPayment
from saas_core.payments.settlement import settle_provider_callback

DSN=os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not DSN,reason="SAAS_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="session",autouse=True)
def migrated_wallet_database():
    root=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema=os.path.join(root,"saas_core","schema")
    with psycopg.connect(DSN,autocommit=True,row_factory=dict_row) as conn:
        for name in ("001_initial.sql","002_payment_lifecycle.sql","003_wallet_referrals.sql",
                     "002_payment_lifecycle.sql","003_wallet_referrals.sql"):
            with open(os.path.join(schema,name),encoding="utf-8") as stream:
                conn.execute(stream.read())
    yield


def connect():
    return psycopg.connect(DSN,row_factory=dict_row)


def make_user(conn):
    uid=uuid4()
    with conn.transaction():
        conn.execute(
            """INSERT INTO users(id,public_id,username,status,phone_e164,phone_verified_at)
               VALUES (%s,%s,%s,'active',%s,now())""",
            (uid,str(uid)[:32],f"wallet-{uid.hex[:16]}",f"+4915{str(uid.int)[:10]}"),
        )
    return uid


def make_verified_bank(conn,user_id):
    bank_id=uuid4()
    conn.execute(
        """INSERT INTO wallet_bank_accounts
           (id,user_id,bank_name,account_fingerprint,encrypted_account_token,
            verification_status,verified_at)
           VALUES (%s,%s,'Sandbox Bank',%s,'encrypted-test-token','verified',now())""",
        (bank_id,user_id,uuid4().hex*2),
    )
    return bank_id


def credit(conn,user_id,bucket,amount,key):
    return post_ledger_entry(
        conn,user_id=user_id,bucket=bucket,entry_type="topup" if bucket=="cash" else "referral_reward",
        amount_toman=amount,idempotency_key=key,description="integration test credit",
    )


def make_paid_subscription(conn, referred_user_id):
    order=create_subscription_order(
        conn,user_id=referred_user_id,plan_code="premium_monthly",
        idempotency_key=f"phase5-order-{uuid4()}",
    )
    attempt=create_payment_attempt(conn,order_id=order["id"],provider="snappay")
    provider_ref=f"phase5-provider-{uuid4()}"
    attach_provider_checkout(
        conn,attempt_id=attempt["id"],provider_order_id=provider_ref,
        redirect_url="https://sandbox.invalid/checkout",request_ref="phase5-test",
    )
    verified=VerifiedPayment(
        provider_event_id=f"phase5-event-{uuid4()}",
        provider_order_id=provider_ref,amount_toman=order["amount_toman"],
        success=True,reference="phase5-reference",
    )
    result=settle_provider_callback(
        conn,order_id=order["id"],attempt_id=attempt["id"],
        provider="snappay",verified=verified,payload_sha256="f"*64,
    )
    return order,result


def test_wallet_spend_is_gift_first_idempotent_and_ledger_immutable(migrated_wallet_database):
    with connect() as conn:
        user=make_user(conn)
        credit(conn,user,"gift",200_000,f"gift-{uuid4()}")
        credit(conn,user,"cash",300_000,f"cash-{uuid4()}")
        ref=uuid4()
        key=f"spend-{uuid4()}"
        first=spend_wallet(conn,user_id=user,amount_toman=250_000,idempotency_key=key,
                           reference_type="test",reference_id=ref)
        retry=spend_wallet(conn,user_id=user,amount_toman=250_000,idempotency_key=key,
                           reference_type="test",reference_id=ref)
        balances=get_wallet_balances(conn,user)
        assert first["already_processed"] is False
        assert retry["already_processed"] is True
        assert balances.gift_toman==0
        assert balances.cash_toman==250_000
        assert sum(row["amount_toman"] for row in first["entries"])==-250_000
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.transaction():
                conn.execute("UPDATE wallet_ledger_entries SET description='tampered' WHERE user_id=%s",(user,))


def test_withdrawal_reserves_cash_then_approval_and_confirmed_payout_debit(migrated_wallet_database):
    with connect() as conn:
        user=make_user(conn)
        reviewer=make_user(conn)
        bank=make_verified_bank(conn,user)
        credit(conn,user,"cash",500_000,f"cash-{uuid4()}")
        request=request_withdrawal(
            conn,user_id=user,amount_toman=200_000,bank_account_id=bank,
            idempotency_key=f"withdraw-{uuid4()}",
        )
        assert get_wallet_balances(conn,user).withdrawable_toman==300_000
        approved=review_withdrawal(
            conn,withdrawal_id=request["id"],reviewer_id=reviewer,
            decision="approved",review_note="Bank account verification reviewed",
        )
        assert approved["status"]=="approved"
        result=settle_withdrawal(
            conn,withdrawal_id=request["id"],reviewer_id=reviewer,
            idempotency_key=f"paid-{request['id']}",payout_reference=f"bank-transfer-{uuid4()}",
        )
        assert result["status"]=="paid"
        balances=get_wallet_balances(conn,user)
        assert balances.cash_toman==300_000
        assert balances.reserved_cash_toman==0
        assert conn.execute(
            "SELECT count(*) AS n FROM admin_audit_logs WHERE target_type='withdrawal' AND target_id=%s",
            (str(request["id"]),),
        ).fetchone()["n"]==2


def test_withdrawal_concurrency_cannot_reserve_more_cash_than_available(migrated_wallet_database):
    with connect() as conn:
        user=make_user(conn)
        bank=make_verified_bank(conn,user)
        credit(conn,user,"cash",100_000,f"cash-{uuid4()}")

    def request_one(i):
        try:
            with connect() as local:
                request_withdrawal(
                    local,user_id=user,amount_toman=70_000,bank_account_id=bank,
                    idempotency_key=f"parallel-withdraw-{uuid4()}",
                )
                return "reserved"
        except ValueError:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(request_one,range(2)))
    assert sorted(outcomes)==["insufficient","reserved"]
    with connect() as conn:
        balances=get_wallet_balances(conn,user)
        assert balances.reserved_cash_toman==70_000
        assert balances.withdrawable_toman==30_000


def test_referral_reward_is_issued_once_from_first_verified_paid_settlement(migrated_wallet_database):
    with connect() as conn:
        inviter=make_user(conn)
        referred=make_user(conn)
        code=ensure_referral_code(conn,inviter)
        attached=attach_referral(conn,referred_user_id=referred,code=code.lower())
        assert attached["status"]=="pending"
        order,result=make_paid_subscription(conn,referred)
        assert result["already_processed"] is False
        balances=get_wallet_balances(conn,inviter)
        assert balances.gift_toman==100_000
        assert conn.execute(
            "SELECT status FROM referrals WHERE referred_user_id=%s",(referred,)
        ).fetchone()["status"]=="rewarded"
        assert conn.execute(
            "SELECT count(*) AS n FROM referral_rewards WHERE user_id=%s",(inviter,)
        ).fetchone()["n"]==1
        assert conn.execute(
            "SELECT count(*) AS n FROM wallet_ledger_entries WHERE user_id=%s AND entry_type='referral_reward'",
            (inviter,),
        ).fetchone()["n"]==1


def test_referral_self_referral_and_duplicate_attachment_are_rejected(migrated_wallet_database):
    with connect() as conn:
        inviter=make_user(conn)
        referred=make_user(conn)
        code=ensure_referral_code(conn,inviter)
        with pytest.raises(ValueError,match="self-referral"):
            attach_referral(conn,referred_user_id=inviter,code=code)
        attach_referral(conn,referred_user_id=referred,code=code)
        with pytest.raises(ValueError,match="already attached"):
            attach_referral(conn,referred_user_id=referred,code=code)


def test_referral_reward_policy_is_versioned_in_database(migrated_wallet_database):
    with connect() as conn:
        row=conn.execute(
            "SELECT amount_toman FROM referral_reward_policies WHERE active=TRUE"
        ).fetchone()
        assert row["amount_toman"]==100_000
