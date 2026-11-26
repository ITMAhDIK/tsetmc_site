"""Wallet persistence and withdrawal reservation rules.

All money is integer Toman. Every mutation targets the isolated SaaS database.
"""
from __future__ import annotations
from uuid import UUID
from saas_core.domain.wallet import WalletBalances, validate_withdrawal
from saas_core.auth.authorization import require_permission


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


def list_wallet_ledger(conn, user_id: UUID, *, limit: int = 50):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,bucket,entry_type,amount_toman,reference_type,reference_id,
                      description,created_at
               FROM wallet_ledger_entries WHERE user_id=%s
               ORDER BY created_at DESC,id DESC LIMIT %s""", (user_id,limit)
        )
        return cur.fetchall()


def post_ledger_entry(conn, *, user_id: UUID, bucket: str, entry_type: str,
                      amount_toman: int, idempotency_key: str,
                      reference_type: str | None = None, reference_id: UUID | None = None,
                      description: str | None = None):
    if bucket not in {"cash","gift"}:
        raise ValueError("invalid wallet bucket")
    if entry_type not in {"topup","referral_reward","purchase_debit","withdrawal_debit",
                          "refund","admin_credit","admin_debit","reversal"}:
        raise ValueError("invalid ledger entry type")
    if isinstance(amount_toman,bool) or not isinstance(amount_toman,int) or amount_toman==0:
        raise ValueError("ledger amount must be a non-zero integer Toman")
    if entry_type in {"topup","referral_reward","refund","admin_credit"} and amount_toman < 0:
        raise ValueError(f"{entry_type} ledger entries must be credits")
    if entry_type in {"purchase_debit","withdrawal_debit","admin_debit"} and amount_toman > 0:
        raise ValueError(f"{entry_type} ledger entries must be debits")
    if not idempotency_key or len(idempotency_key)>120:
        raise ValueError("valid idempotency key is required")
    with conn.transaction():
        _lock_wallet(conn,user_id)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,user_id,bucket,entry_type,amount_toman,reference_type,reference_id
                   FROM wallet_ledger_entries WHERE idempotency_key=%s""",(idempotency_key,)
            )
            existing=cur.fetchone()
            expected=(user_id,bucket,entry_type,amount_toman,reference_type,reference_id)
            if existing:
                actual=tuple(existing[k] for k in
                    ("user_id","bucket","entry_type","amount_toman","reference_type","reference_id"))
                if actual!=expected:
                    raise ValueError("idempotency key was reused with different ledger data")
                return existing
            cur.execute(
                """SELECT COALESCE(SUM(amount_toman),0)::bigint AS balance
                   FROM wallet_ledger_entries WHERE user_id=%s AND bucket=%s""",
                (user_id,bucket),
            )
            balance=cur.fetchone()["balance"]
            if balance+amount_toman<0:
                raise ValueError("wallet debit exceeds available bucket balance")
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
            row=cur.fetchone()
            if row:
                return row
            cur.execute(
                """SELECT id,user_id,bucket,entry_type,amount_toman,reference_type,reference_id
                   FROM wallet_ledger_entries WHERE idempotency_key=%s""",(idempotency_key,)
            )
            existing=cur.fetchone()
            actual=tuple(existing[k] for k in
                ("user_id","bucket","entry_type","amount_toman","reference_type","reference_id"))
            if actual!=expected:
                raise ValueError("idempotency key was reused with different ledger data")
            return existing


def request_withdrawal(conn, *, user_id: UUID, amount_toman: int,
                       bank_account_id: UUID, idempotency_key: str):
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int):
        raise TypeError("withdrawal amount must be integer Toman")
    if not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("valid idempotency key is required")
    with conn.transaction():
        _lock_wallet(conn, user_id)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,user_id,amount_toman,bank_account_id,status
                   FROM withdrawal_requests WHERE idempotency_key=%s FOR UPDATE""",
                (idempotency_key,),
            )
            existing=cur.fetchone()
            if existing:
                if (existing["user_id"] != user_id or existing["amount_toman"] != amount_toman
                        or existing["bank_account_id"] != bank_account_id):
                    raise ValueError("idempotency key was reused with different withdrawal data")
                return existing
        balances = get_wallet_balances(conn, user_id)
        validate_withdrawal(balances, amount_toman)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM wallet_bank_accounts
                   WHERE id=%s AND user_id=%s AND verification_status='verified' FOR SHARE""",
                (bank_account_id,user_id),
            )
            if not cur.fetchone():
                raise PermissionError("a verified bank account is required")
            cur.execute(
                """INSERT INTO withdrawal_requests
                   (user_id,amount_toman,status,bank_account_token,bank_account_id,idempotency_key)
                   VALUES (%s,%s,'requested',%s,%s,%s)
                   RETURNING id,user_id,amount_toman,status,requested_at""",
                (user_id,amount_toman,"vault-reference:"+str(bank_account_id),
                 bank_account_id,idempotency_key),
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


def settle_withdrawal(conn, *, withdrawal_id: UUID, reviewer_id: UUID,
                      idempotency_key: str, payout_reference: str):
    """Record cash debit only after an external payout has been confirmed."""
    if not payout_reference or len(payout_reference)>160:
        raise ValueError("a valid external payout reference is required")
    with conn.transaction():
        require_permission(conn,reviewer_id,"withdrawals.review")
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM withdrawal_requests WHERE id=%s",(withdrawal_id,))
            owner=cur.fetchone()
            if not owner:
                raise LookupError("withdrawal request not found")
            _lock_wallet(conn,owner["user_id"])
            cur.execute(
                """SELECT user_id,amount_toman,status,payout_reference FROM withdrawal_requests
                   WHERE id=%s FOR UPDATE""",(withdrawal_id,)
            )
            request=cur.fetchone()
            if not request:
                raise LookupError("withdrawal request not found")
            if request["status"]=="paid":
                cur.execute(
                    """SELECT id FROM wallet_ledger_entries WHERE idempotency_key=%s
                       AND user_id=%s AND bucket='cash' AND entry_type='withdrawal_debit'
                       AND amount_toman=%s AND reference_type='withdrawal' AND reference_id=%s""",
                    (idempotency_key,request["user_id"],-request["amount_toman"],withdrawal_id),
                )
                prior=cur.fetchone()
                if prior and request["payout_reference"] == payout_reference:
                    return {"withdrawal_id":withdrawal_id,"status":"paid","already_processed":True}
                raise ValueError("withdrawal was already settled with different payout data")
            if request["status"]!="approved":
                raise ValueError("only approved withdrawals can be settled")
            post_ledger_entry(
                conn,user_id=request["user_id"],bucket="cash",entry_type="withdrawal_debit",
                amount_toman=-request["amount_toman"],idempotency_key=idempotency_key,
                reference_type="withdrawal",reference_id=withdrawal_id,
                description="Confirmed bank withdrawal",
            )
            cur.execute(
                """UPDATE withdrawal_requests SET status='paid',paid_at=now(),
                   reviewed_at=COALESCE(reviewed_at,now()),reviewed_by=%s,payout_reference=%s
                   WHERE id=%s AND status='approved'""",
                (reviewer_id,payout_reference,withdrawal_id),
            )
            cur.execute(
                """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                   VALUES (%s,'wallet.withdrawal.paid','withdrawal',%s,
                           jsonb_build_object('payout_reference',%s::text))""",
                (reviewer_id,str(withdrawal_id),payout_reference),
            )
            return {"withdrawal_id":withdrawal_id,"status":"paid"}


def spend_wallet(conn, *, user_id: UUID, amount_toman: int, idempotency_key: str,
                 reference_type: str, reference_id: UUID, description: str | None = None):
    """Atomically spend gift first, then unreserved cash, using append-only debits."""
    from saas_core.domain.wallet import spend_buckets
    if not isinstance(amount_toman,int) or isinstance(amount_toman,bool) or amount_toman<=0:
        raise ValueError("spend amount must be a positive integer Toman")
    if not idempotency_key or len(idempotency_key)>100:
        raise ValueError("valid idempotency key is required")
    if not reference_type or len(reference_type)>32:
        raise ValueError("valid spend reference type is required")
    with conn.transaction():
        _lock_wallet(conn,user_id)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,user_id,amount_toman,reference_type,reference_id
                   FROM wallet_ledger_entries
                   WHERE idempotency_key IN (%s,%s) AND entry_type='purchase_debit'""",
                (idempotency_key+":gift",idempotency_key+":cash"),
            )
            prior=cur.fetchall()
            if prior:
                if (any(row["user_id"]!=user_id or row["reference_type"]!=reference_type
                        or row["reference_id"]!=reference_id for row in prior)
                        or -sum(row["amount_toman"] for row in prior)!=amount_toman):
                    raise ValueError("idempotency key was reused with different wallet spend data")
                return {"already_processed":True,"entries":prior}
        balances=get_wallet_balances(conn,user_id)
        spendable=WalletBalances(balances.withdrawable_toman,balances.gift_toman)
        remaining=spend_buckets(spendable,amount_toman)
        gift_debit=remaining.gift_toman-spendable.gift_toman
        cash_debit=remaining.cash_toman-spendable.cash_toman
        entries=[]
        if gift_debit:
            entries.append(post_ledger_entry(
                conn,user_id=user_id,bucket="gift",entry_type="purchase_debit",
                amount_toman=gift_debit,idempotency_key=idempotency_key+":gift",
                reference_type=reference_type,reference_id=reference_id,description=description,
            ))
        if cash_debit:
            entries.append(post_ledger_entry(
                conn,user_id=user_id,bucket="cash",entry_type="purchase_debit",
                amount_toman=cash_debit,idempotency_key=idempotency_key+":cash",
                reference_type=reference_type,reference_id=reference_id,description=description,
            ))
        return {"already_processed":False,"entries":entries}


def review_withdrawal(conn, *, withdrawal_id: UUID, reviewer_id: UUID,
                      decision: str, review_note: str):
    if decision not in {"approved","rejected"}:
        raise ValueError("withdrawal decision must be approved or rejected")
    note=(review_note or "").strip()
    if not note or len(note)>500:
        raise ValueError("a review note between 1 and 500 characters is required")
    with conn.transaction():
        require_permission(conn,reviewer_id,"withdrawals.review")
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM withdrawal_requests WHERE id=%s",(withdrawal_id,))
            owner=cur.fetchone()
            if not owner:
                raise LookupError("withdrawal request not found")
            _lock_wallet(conn,owner["user_id"])
            cur.execute(
                """UPDATE withdrawal_requests SET status=%s,reviewed_at=now(),
                   reviewed_by=%s,review_note=%s
                   WHERE id=%s AND status='requested'
                   RETURNING id,user_id,amount_toman,status""",
                (decision,reviewer_id,note,withdrawal_id),
            )
            row=cur.fetchone()
            if not row:
                raise ValueError("withdrawal is not awaiting review")
            cur.execute(
                """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                   VALUES (%s,%s,'withdrawal',%s,
                           jsonb_build_object('status',%s::text,'amount_toman',%s,'note',%s::text))""",
                (reviewer_id,f"wallet.withdrawal.{decision}",str(withdrawal_id),
                 decision,row["amount_toman"],note),
            )
            return row
