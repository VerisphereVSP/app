"""app/invites.py — patch_invites (v2 design rev 2, step 4a: sponsored invitations)

A player puts VSP on the table for someone: the amount leaves the sponsor's wallet at once (a signed EIP-2612 permit
to the escrow address + transferFrom, gas paid by the relay), a code comes back, and whoever redeems the code receives
the VSP in their own wallet. Unredeemed by its expiry, or revoked by the sponsor, the VSP goes back.

  GET  /api/invites/config          escrow address + the knobs (the client needs the spender for the permit)
  POST /api/invites                 {amount_vsp, expires_days, permit} → {code, …}         (sponsor = permit.owner)
  GET  /api/invites/peek?code=      {status, amount_vsp, expires_at}  — no sponsor; what the share page shows
  POST /api/invites/redeem          {code, address} → {amount_vsp, tx_hash}
  POST /api/invites/revoke          {code_hash, ts, signature} — personal_sign by the sponsor → refund
  GET  /api/invites?sponsor=0x…     the sponsor's codes (amounts and states; never the plaintext code)
  GET  /api/invites/escrow          reconcile: open escrow total vs the escrow wallet's VSP balance (ops proof)

Escrow for the beta is custodial, in the relay wallet (KMS-signed EOA that already pays gas and used to collect fees);
every movement is a plain ERC-20 transfer on chain and a row here. An on-chain escrow contract is the v3 upgrade.
Knobs (env, read at startup): INVITE_MIN_VSP 0.5, INVITE_MAX_VSP 100, INVITE_MAX_DAYS 365, INVITE_MAX_OUTSTANDING 100.
Redemption rules: an open, unexpired code; a wallet that holds no VSP and has no stake yet; not the sponsor.
The chain side is behind three module functions (escrow_pull / escrow_pay / vsp_balance) so the rest is unit-testable.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

WEI = 10 ** 18


def _dec_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except InvalidOperation:
        return Decimal(default)


INVITE_MIN_VSP = _dec_env("INVITE_MIN_VSP", "0.5")
INVITE_MAX_VSP = _dec_env("INVITE_MAX_VSP", "100")
INVITE_MAX_DAYS = int(os.getenv("INVITE_MAX_DAYS", "365"))
INVITE_MAX_OUTSTANDING = int(os.getenv("INVITE_MAX_OUTSTANDING", "100"))
REVOKE_SIGNATURE_WINDOW_SEC = 600

_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # Crockford-like: no 0/O/1/I, 32 symbols → 5 bits each


# ── codes ────────────────────────────────────────────────────────────────────────────────────────────

def new_code() -> str:
    """VSP-XXXX-XXXX-XXXX: 60 random bits, unambiguous letters, easy to read aloud."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"VSP-{body[:4]}-{body[4:8]}-{body[8:]}"


def normalize_code(code: str) -> str:
    """Accepts what a human typed or pasted: any case, with or without dashes/spaces or the VSP prefix. The alphabet
    has no 0/O/1/I/L, so a code containing them was mistyped and simply will not match."""
    s = (code or "").strip().upper().replace(" ", "").replace("-", "")
    if s.startswith("VSP"):
        s = s[3:]
    if len(s) != 12 or any(c not in _ALPHABET for c in s):
        raise HTTPException(400, "That does not look like an invitation code (VSP-XXXX-XXXX-XXXX).")
    return f"VSP-{s[:4]}-{s[4:8]}-{s[8:]}"


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ── chain side (replaced in tests) ──────────────────────────────────────────────────────────────────

_ERC20_ABI = [
    {"inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "name": "transferFrom", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def escrow_address() -> str:
    from relay_wallet import account
    return account.address


def _token():
    from web3 import Web3
    from relay_wallet import w3
    from config import VSP_TOKEN_ADDRESS
    return w3.eth.contract(address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS), abi=_ERC20_ABI)


def escrow_pull(sponsor: str, amount_wei: int, permit) -> str:
    """permit(sponsor → escrow) then transferFrom(sponsor, escrow, amount). Returns the transferFrom tx hash."""
    from web3 import Web3
    from relay import _constrain_permit, _execute_permit
    from relay_wallet import w3, account, sign_and_send
    _constrain_permit(permit, sponsor)
    if (permit.spender or "").lower() != account.address.lower():
        raise HTTPException(400, "Permit spender must be the escrow address")
    if int(permit.value) < amount_wei:
        raise HTTPException(400, "Permit value is below the invitation amount")
    _execute_permit(permit)
    tx = _token().functions.transferFrom(Web3.to_checksum_address(sponsor), account.address, amount_wei) \
        .build_transaction({"from": account.address, "gas": 120_000})
    tx_hash = sign_and_send(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status == 0:
        raise HTTPException(502, "Escrow transfer reverted on-chain")
    return tx_hash if isinstance(tx_hash, str) else tx_hash.hex()


def escrow_pay(to: str, amount_wei: int) -> str:
    """transfer(escrow → to, amount). Returns the tx hash."""
    from web3 import Web3
    from relay_wallet import w3, account, sign_and_send
    tx = _token().functions.transfer(Web3.to_checksum_address(to), amount_wei) \
        .build_transaction({"from": account.address, "gas": 120_000})
    tx_hash = sign_and_send(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status == 0:
        raise HTTPException(502, "Escrow payout reverted on-chain")
    return tx_hash if isinstance(tx_hash, str) else tx_hash.hex()


def vsp_balance(address: str) -> int:
    from web3 import Web3
    return int(_token().functions.balanceOf(Web3.to_checksum_address(address)).call())


# ── data ─────────────────────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v) -> str | None:
    if v is None:
        return None
    v = _as_dt(v)
    return v.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _vsp(amount_wei) -> str:
    d = Decimal(int(amount_wei)) / WEI
    s = f"{d.normalize():f}"
    return s if "." in s else s


def _row(r) -> dict:
    m = r._mapping if hasattr(r, "_mapping") else r
    return {
        "id": int(m["id"]), "code_hash": m["code_hash"], "sponsor": m["sponsor"], "amount_vsp": _vsp(m["amount_wei"]),
        "amount_wei": str(int(m["amount_wei"])), "created_at": _iso(m["created_at"]), "expires_at": _iso(m["expires_at"]),
        "status": m["status"], "redeemed_by": m["redeemed_by"], "redeemed_at": _iso(m["redeemed_at"]),
        "escrow_tx": m["escrow_tx"], "redeem_tx": m["redeem_tx"], "refund_tx": m["refund_tx"], "closed_at": _iso(m["closed_at"]),
    }


def _get_by_hash(db: Session, h: str):
    r = db.execute(sql_text("SELECT * FROM invite_code WHERE code_hash = :h"), {"h": h}).first()
    return _row(r) if r else None


def open_count(db: Session, sponsor: str) -> int:
    return int(db.execute(sql_text("SELECT COUNT(*) FROM invite_code WHERE sponsor = :s AND status = 'open'"),
                          {"s": sponsor.lower()}).scalar() or 0)


def open_total_wei(db: Session) -> int:
    rows = db.execute(sql_text("SELECT amount_wei FROM invite_code WHERE status = 'open'")).all()
    return sum(int(r[0]) for r in rows)


def _has_stake(db: Session, address: str) -> bool:
    try:
        n = db.execute(sql_text("SELECT COUNT(*) FROM chain_user_stake WHERE lower(user_address) = :a AND amount > 0"),
                       {"a": address.lower()}).scalar()
        return int(n or 0) > 0
    except Exception:  # table absent in a bare test DB
        db.rollback()
        return False


def _close(db: Session, h: str, status: str, refund_tx: str | None) -> None:
    db.execute(sql_text("UPDATE invite_code SET status = :st, closed_at = :now, refund_tx = :tx WHERE code_hash = :h AND status = 'open'"),
               {"st": status, "now": _now(), "tx": refund_tx, "h": h})
    db.commit()


def expire_due(db: Session, limit: int = 50) -> int:
    """Refund open codes past their expiry. Called by the worker hourly and lazily by the list/peek routes."""
    rows = db.execute(sql_text("SELECT * FROM invite_code WHERE status = 'open'")).all()
    now = _now(); n = 0
    for r in rows:
        row = _row(r)
        if _as_dt(row["expires_at"]) <= now:
            try:
                tx = escrow_pay(row["sponsor"], int(row["amount_wei"]))
            except Exception as e:
                logger.warning("invite expiry refund failed for %s: %s", row["code_hash"][:12], e)
                continue
            _close(db, row["code_hash"], "expired", tx)
            n += 1
            if n >= limit:
                break
    return n


# ── routes ───────────────────────────────────────────────────────────────────────────────────────────

class PermitIn(BaseModel):
    token: str
    owner: str
    spender: str
    value: str
    deadline: int
    v: int
    r: str
    s: str


class IssueIn(BaseModel):
    amount_vsp: str
    expires_days: int = 30
    permit: PermitIn


class RedeemIn(BaseModel):
    code: str
    address: str


class RevokeIn(BaseModel):
    code_hash: str
    ts: int
    signature: str


def revoke_message(h: str, ts: int) -> str:
    return f"Verisphere invitation revoke\ncode_hash: {h}\nts: {ts}"


def _is_address(a: str) -> bool:
    return isinstance(a, str) and len(a) == 42 and a.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in a[2:])


@router.get("/api/invites/config")
def invites_config():
    return {"escrow_address": escrow_address(), "min_vsp": str(INVITE_MIN_VSP), "max_vsp": str(INVITE_MAX_VSP),
            "max_days": INVITE_MAX_DAYS, "max_outstanding": INVITE_MAX_OUTSTANDING, "default_days": 30}


@router.post("/api/invites")
def invites_issue(body: IssueIn, db: Session = Depends(get_db)):
    try:
        amount = Decimal(body.amount_vsp)
    except InvalidOperation:
        raise HTTPException(400, "amount_vsp must be a number")
    if not (INVITE_MIN_VSP <= amount <= INVITE_MAX_VSP):
        raise HTTPException(400, f"Amount must be between {INVITE_MIN_VSP} and {INVITE_MAX_VSP} VSP")
    if not (1 <= body.expires_days <= INVITE_MAX_DAYS):
        raise HTTPException(400, f"Expiry must be between 1 and {INVITE_MAX_DAYS} days")
    sponsor = (body.permit.owner or "").lower()
    if not _is_address(sponsor):
        raise HTTPException(400, "Permit owner must be an address")
    if open_count(db, sponsor) >= INVITE_MAX_OUTSTANDING:
        raise HTTPException(429, f"You already have {INVITE_MAX_OUTSTANDING} open invitations; revoke or wait for some to be redeemed")
    amount_wei = int(amount * WEI)
    tx = escrow_pull(sponsor, amount_wei, body.permit)
    code = new_code()
    h = code_hash(code)
    expires = _now() + timedelta(days=body.expires_days)
    db.execute(sql_text(
        "INSERT INTO invite_code (code_hash, sponsor, amount_wei, created_at, expires_at, escrow_tx, status) "
        "VALUES (:h, :s, :a, :c, :e, :tx, 'open')"),
        {"h": h, "s": sponsor, "a": str(amount_wei), "c": _now(), "e": expires, "tx": tx})
    db.commit()
    _tx_log(db, tx, sponsor, "invite_escrow", amount)
    return {"code": code, "code_hash": h, "amount_vsp": _vsp(amount_wei), "expires_at": _iso(expires), "escrow_tx": tx,
            "sponsor": sponsor}


@router.get("/api/invites/peek")
def invites_peek(code: str = Query(...), db: Session = Depends(get_db)):
    row = _get_by_hash(db, code_hash(normalize_code(code)))
    if not row:
        raise HTTPException(404, "No such invitation")
    status = row["status"]
    if status == "open" and _as_dt(row["expires_at"]) <= _now():
        status = "expired"
    return {"status": status, "amount_vsp": row["amount_vsp"], "expires_at": row["expires_at"]}


@router.post("/api/invites/redeem")
def invites_redeem(body: RedeemIn, db: Session = Depends(get_db)):
    addr = (body.address or "").lower()
    if not _is_address(addr):
        raise HTTPException(400, "address must be a wallet address")
    h = code_hash(normalize_code(body.code))
    row = _get_by_hash(db, h)
    if not row:
        raise HTTPException(404, "No such invitation")
    if row["status"] != "open":
        raise HTTPException(409, f"This invitation was already {row['status']}")
    if _as_dt(row["expires_at"]) <= _now():
        raise HTTPException(409, "This invitation has expired")
    if addr == row["sponsor"]:
        raise HTTPException(400, "An invitation cannot be redeemed by its sponsor")
    if vsp_balance(addr) > 0 or _has_stake(db, addr):
        raise HTTPException(409, "Invitations are for newcomers: this wallet already holds or has staked VSP")
    # claim the row first (one redemption per code even under concurrent requests), then pay
    n = db.execute(sql_text(
        "UPDATE invite_code SET status = 'redeemed', redeemed_by = :a, redeemed_at = :now WHERE code_hash = :h AND status = 'open'"),
        {"a": addr, "now": _now(), "h": h}).rowcount
    db.commit()
    if n != 1:
        raise HTTPException(409, "This invitation was just redeemed")
    try:
        tx = escrow_pay(addr, int(row["amount_wei"]))
    except Exception as e:
        db.execute(sql_text("UPDATE invite_code SET status = 'open', redeemed_by = NULL, redeemed_at = NULL WHERE code_hash = :h"), {"h": h})
        db.commit()
        raise HTTPException(502, f"Payout failed, the invitation is still open: {str(e)[:200]}")
    db.execute(sql_text("UPDATE invite_code SET redeem_tx = :tx WHERE code_hash = :h"), {"tx": tx, "h": h})
    db.commit()
    _tx_log(db, tx, addr, "invite_redeem", Decimal(int(row["amount_wei"])) / WEI)
    return {"amount_vsp": row["amount_vsp"], "tx_hash": tx, "status": "redeemed"}


@router.post("/api/invites/revoke")
def invites_revoke(body: RevokeIn, db: Session = Depends(get_db)):
    row = _get_by_hash(db, body.code_hash)
    if not row:
        raise HTTPException(404, "No such invitation")
    if abs(int(time.time()) - int(body.ts)) > REVOKE_SIGNATURE_WINDOW_SEC:
        raise HTTPException(400, "Signature is too old; sign again")
    from eth_account import Account
    from eth_account.messages import encode_defunct
    try:
        signer = Account.recover_message(encode_defunct(text=revoke_message(body.code_hash, body.ts)), signature=body.signature).lower()
    except Exception:
        raise HTTPException(400, "Bad signature")
    if signer != row["sponsor"]:
        raise HTTPException(403, "Only the sponsor can revoke this invitation")
    if row["status"] != "open":
        raise HTTPException(409, f"This invitation was already {row['status']}")
    tx = escrow_pay(row["sponsor"], int(row["amount_wei"]))
    _close(db, body.code_hash, "revoked", tx)
    _tx_log(db, tx, row["sponsor"], "invite_refund", Decimal(int(row["amount_wei"])) / WEI)
    return {"status": "revoked", "refund_tx": tx, "amount_vsp": row["amount_vsp"]}


@router.get("/api/invites")
def invites_list(sponsor: str = Query(...), db: Session = Depends(get_db)):
    s = (sponsor or "").lower()
    if not _is_address(s):
        raise HTTPException(400, "sponsor must be a wallet address")
    expire_due(db)
    rows = db.execute(sql_text("SELECT * FROM invite_code WHERE sponsor = :s ORDER BY created_at DESC LIMIT 200"), {"s": s}).all()
    out = []
    for r in rows:
        d = _row(r)
        d.pop("sponsor", None)
        out.append(d)
    return {"sponsor": s, "open": sum(1 for d in out if d["status"] == "open"), "max_outstanding": INVITE_MAX_OUTSTANDING, "invites": out}


@router.get("/api/invites/escrow")
def invites_escrow(db: Session = Depends(get_db)):
    """Reconcile: the escrow wallet must hold at least the open total (it may hold more: legacy fee VSP)."""
    open_wei = open_total_wei(db)
    bal = vsp_balance(escrow_address())
    return {"escrow_address": escrow_address(), "open_vsp": _vsp(open_wei), "escrow_balance_vsp": _vsp(bal), "covered": bal >= open_wei}


def _tx_log(db: Session, tx_hash: str, user: str, action: str, amount_vsp: Decimal) -> None:
    """Show the movement in the user's Transactions view (the watcher confirms it). Best effort."""
    try:
        from config import VSP_TOKEN_ADDRESS
        from tx_log import record_pending
        record_pending(db, tx_hash, user, VSP_TOKEN_ADDRESS or "0x", "0x", action, float(amount_vsp))
        db.commit()
    except Exception:
        db.rollback()
        logger.debug("tx_log skipped for %s", action, exc_info=True)
