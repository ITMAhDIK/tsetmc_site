"""Wallet persistence and withdrawal reservation rules.

All money is integer Toman. Every mutation targets the isolated SaaS database.
"""
from __future__ import annotations
from uuid import UUID
from saas_core.domain.wallet import WalletBalances, validate_withdrawal


def _lock_wallet(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO wallet_accounts(user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )
        cur.execute("SELECT user_id FROM wallet_accounts WHERE user_id=%s FOR UPDATE", (user_id,))
        if cur.fetchone() is None:
            raise LookupError("wallet account not found")


def get_wallet_balances(conn, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                 COALESCE(SUM(amount_toman) FILTER (WHERE bucket='cash'),0)::bigint AS cash,
                 COALESCE(SUM(amount_toman) FILTER (WHERE bucket='gift'),0)::bigint AS gift
               FROM wallet_ledger_entries WHERE user_id=%s""", (user_id,)
        )
        row = cur.fetchone()
        cur.execute(
            """SELECT COALESCE(SUM(amount_toman),0)::bigint AS reserved
               FROM withdrawal_requests
               WHERE user_id=%s AND status IN ('requested','approved')""", (user_id,)
        )
        reserved = cur.fetchone()["reserved"]
    return WalletBalances(row["cash"], row["gift"], reserved)


def list_wallet_ledger(conn, user_id: UUID, *, limit: int = 50, before_id: UUID | None = None):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,bucket,entry_type,amount_toman,reference_type,reference_id,
                      description,created_at
               FROM wallet_ledger_entries
               WHERE user_id=%s AND (%s IS NULL OR created_at <
                 (SELECT created_at FROM wallet_ledger_entries WHERE id=%s AND user_id=%s))
               ORDER BY created_at DESC,id DESC LIMIT %s""",
            (user_id,before_id,before_id,user_id,limit),
        )
        return cur.fetchall()


def post_ledger_entry(conn, *, user_id: UUID, bucket: str, entry_type: str,
                      amount_toman: int, idempotency_key: str,
                      reference_type: str | None = None, reference_id: UUID | None = None,
                      description: str | None = None):
    if bucket not in {"cash", "gift"}:
        raise ValueError("invalid wallet bucket")
    if entry_type not in {"topup","referral_reward","purchase_debit","withdrawal_debit",
                          "refund","admin_credit","admin_debit","reversal"}:
        raise ValueError("invalid ledger entry type")
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int) or amount_toman == 0:
        raise ValueError("ledger amount must be a non-zero integer Toman")
    if not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("valid idempotency key is required")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO wallet_ledger_entries
               (user_id,bucket,entry_type,amount_toman,reference_type,reference_id,
                idempotency_key,description)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id,user_id,bucket,entry_type,amount_toman,reference_type,reference_id""",
            (user_id,bucket,entry_type,amount_toman,reference_type,reference_id,
             idempotency_key,description),
        )
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            """SELECT id,user_id,bucket,entry_type,amount_toman,reference_type,reference_id
               FROM wallet_ledger_entries WHERE idempotency_key=%s""", (idempotency_key,)
        )
        existing = cur.fetchone()
        expected = (user_id,bucket,entry_type,amount_toman,reference_type,reference_id)
        actual = tuple(existing[k] for k in
                       ("user_id","bucket","entry_type","amount_toman","reference_type","reference_id"))
        if actual != expected:
            raise ValueError("idempotency key was reused with different ledger data")
        return existing


def request_withdrawal(conn, *, user_id: UUID, amount_toman: int,
                       bank_account_id: UUID, idempotency_key: str):
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int):
        raise TypeError("withdrawal amount must be integer Toman")
    with conn.transaction():
        _lock_wallet(conn, user_id)
        balances = get_wallet_balances(conn, user_id)
        validate_withdrawal(balances, amount_toman)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM wallet_bank_accounts
                   WHERE id=%s AND user_id=%s AND verification_status='verified'""",
                (bank_account_id,user_id),
            )
            if not cur.fetchone():
                raise PermissionError("a verified bank account is required")
            cur.execute(
                """INSERT INTO withdrawal_requests
                   (user_id,amount_toman,status,bank_account_token,bank_account_id)
                   VALUES (%s,%s,'requested','vault-reference:%s',%s)
                   RETURNING id,user_id,amount_toman,status,requested_at""",
                (user_id,amount_toman,str(bank_account_id),bank_account_id),
            )
            return cur.fetchone()


def cancel_withdrawal(conn, *, user_id: UUID, withdrawal_id: UUID):
    with conn.transaction():
        _lock_wallet(conn,user_id)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE withdrawal_requests SET status='cancelled'
                   WHERE id=%s AND user_id=%s AND status='requested'
                   RETURNING id,status""", (withdrawal_id,user_id)
            )
            return cur.fetchone()


def settle_withdrawal(conn, *, withdrawal_id: UUID, reviewer_id: UUID, idempotency_key: str):
    """Record the cash debit only after an approved payout is confirmed paid."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id,amount_toman,status FROM withdrawal_requests
                   WHERE id=%s FOR UPDATE""", (withdrawal_id,)
            )
            request = cur.fetchone()
            if not request:
                raise LookupError("withdrawal request not found")
            _lock_wallet(conn,request["user_id"])
            if request["status"] != "approved":
                raise ValueError("only approved withdrawals can be settled")
            post_ledger_entry(
                conn,user_id=request["user_id"],bucket="cash",entry_type="withdrawal_debit",
                amount_toman=-request["amount_toman"],idempotency_key=idempotency_key,
                reference_type="withdrawal",reference_id=withdrawal_id,
                description="Confirmed bank withdrawal",
            )
            cur.execute(
                """UPDATE withdrawal_requests SET status='paid',paid_at=now(),
                   reviewed_at=COALESCE(reviewed_at,now()),reviewed_by=%s
                   WHERE id=%s AND status='approved'""", (reviewer_id,withdrawal_id)
            )
            return {"withdrawal_id":withdrawal_id,"status":"paid"}
