# app/tests/test_invites.py — patch_invites
"""
Sponsored invitations against a real (sqlite) session running the migration's DDL (adapted for sqlite types) with the
three chain functions replaced by a fake escrow ledger. No RPC, no KMS, no Postgres.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import invites  # noqa: E402
from db import get_db  # noqa: E402

ESCROW = "0x000000000000000000000000000000000000e5c0"
SPONSOR_KEY = "0x" + "11" * 32
NEWBIE = "0x00000000000000000000000000000000000000ab"


def sqlite_ddl() -> str:
    raw = (ROOT / "ops" / "compose" / "migrations" / "350_invite_code.sql").read_text() + (ROOT / "ops" / "compose" / "migrations" / "351_invite_code_post.sql").read_text()
    ddl = "\n".join(l for l in raw.splitlines() if not l.startswith("--")).replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
    ddl = ddl.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    ddl = ddl.replace("TIMESTAMPTZ NOT NULL DEFAULT now()", "TEXT NOT NULL DEFAULT (datetime('now'))")
    ddl = ddl.replace("TIMESTAMPTZ", "TEXT").replace("NUMERIC(78,0)", "TEXT")
    return ddl


class FakeChain:
    """What the escrow wallet and the token would do, as a ledger."""
    def __init__(self):
        self.balances = {}          # address → wei
        self.pulls, self.pays = [], []
        self.fail_pay = False
        self.n = 0

    def tx(self):
        self.n += 1
        return "0x" + f"{self.n:064x}"

    def escrow_pull(self, sponsor, amount_wei, permit):
        from fastapi import HTTPException
        if permit.owner.lower() != sponsor or permit.spender.lower() != ESCROW or int(permit.value) < amount_wei:
            raise HTTPException(400, "bad permit")  # what the real escrow_pull does via _constrain_permit
        self.pulls.append((sponsor, amount_wei))
        self.balances[ESCROW] = self.balances.get(ESCROW, 0) + amount_wei
        return self.tx()

    def escrow_pay(self, to, amount_wei):
        if self.fail_pay:
            raise RuntimeError("rpc down")
        self.pays.append((to.lower(), amount_wei))
        self.balances[ESCROW] -= amount_wei
        self.balances[to.lower()] = self.balances.get(to.lower(), 0) + amount_wei
        return self.tx()

    def vsp_balance(self, address):
        return self.balances.get(address.lower(), 0)


@pytest.fixture
def chain(monkeypatch):
    c = FakeChain()
    monkeypatch.setattr(invites, "escrow_pull", c.escrow_pull)
    monkeypatch.setattr(invites, "escrow_pay", c.escrow_pay)
    monkeypatch.setattr(invites, "vsp_balance", c.vsp_balance)
    monkeypatch.setattr(invites, "escrow_address", lambda: ESCROW)
    monkeypatch.setattr(invites, "_tx_log", lambda *a, **k: None)
    return c


@pytest.fixture
def db_and_client():
    eng = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        for stmt in sqlite_ddl().split(";"):
            if stmt.strip():
                c.execute(text(stmt))
        c.execute(text("CREATE TABLE chain_user_stake (user_address TEXT, post_id INTEGER, side INTEGER, amount REAL)"))
    SessionLocal = sessionmaker(bind=eng, future=True)
    app = FastAPI()
    app.include_router(invites.router)
    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _db
    return SessionLocal, TestClient(app)


def sponsor_account():
    from eth_account import Account
    return Account.from_key(SPONSOR_KEY)


def permit_for(amount_vsp: str, spender=ESCROW):
    acct = sponsor_account()
    return {"token": "0x0000000000000000000000000000000000000001", "owner": acct.address, "spender": spender,
            "value": str(int(float(amount_vsp) * 10 ** 18)), "deadline": int(time.time()) + 3600, "v": 27, "r": "0x" + "00" * 32, "s": "0x" + "00" * 32}


def issue(client, amount="2", days=30, post_id=None):
    r = client.post("/api/invites", json={"amount_vsp": amount, "expires_days": days, "permit": permit_for(amount), "post_id": post_id})
    assert r.status_code == 200, r.text
    return r.json()


class TestCodes:
    def test_format_and_normalisation(self):
        c = invites.new_code()
        assert len(c) == 18 and c.startswith("VSP-") and all(ch in invites._ALPHABET for ch in c.replace("VSP-", "").replace("-", ""))
        assert invites.normalize_code(c.lower().replace("-", " ")) == c
        assert invites.normalize_code(c.replace("VSP-", "")) == c
        for bad in ("VSP-1111-2222-3333", "hello", "", "VSP-ABCD-EFGH"):
            with pytest.raises(Exception):
                invites.normalize_code(bad)
        assert invites.code_hash(c) != invites.code_hash(invites.new_code())


class TestIssue:
    def test_config(self, chain, db_and_client):
        _, c = db_and_client
        d = c.get("/api/invites/config").json()
        assert d["escrow_address"] == ESCROW and d["min_vsp"] == "0.5" and d["max_vsp"] == "100" and d["max_days"] == 365 and d["max_outstanding"] == 100

    def test_issue_escrows_and_returns_a_code(self, chain, db_and_client):
        _, c = db_and_client
        d = issue(c, "2.5", 10)
        assert d["code"].startswith("VSP-") and d["amount_vsp"] == "2.5" and d["sponsor"] == sponsor_account().address.lower()
        assert chain.pulls == [(sponsor_account().address.lower(), 2_500_000_000_000_000_000)]
        assert chain.balances[ESCROW] == 2_500_000_000_000_000_000
        exp = datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00"))
        assert timedelta(days=9, hours=23) < exp - datetime.now(timezone.utc) <= timedelta(days=10)
        lst = c.get(f"/api/invites?sponsor={sponsor_account().address}").json()
        assert lst["open"] == 1 and lst["invites"][0]["status"] == "open" and "code" not in lst["invites"][0]
        assert lst["invites"][0]["code_hash"] == d["code_hash"]  # the hash, never the plaintext

    def test_knobs(self, chain, db_and_client, monkeypatch):
        _, c = db_and_client
        assert c.post("/api/invites", json={"amount_vsp": "0.1", "expires_days": 30, "permit": permit_for("0.1")}).status_code == 400
        assert c.post("/api/invites", json={"amount_vsp": "101", "expires_days": 30, "permit": permit_for("101")}).status_code == 400
        assert c.post("/api/invites", json={"amount_vsp": "1", "expires_days": 400, "permit": permit_for("1")}).status_code == 400
        assert c.post("/api/invites", json={"amount_vsp": "abc", "expires_days": 30, "permit": permit_for("1")}).status_code == 400
        monkeypatch.setattr(invites, "INVITE_MAX_OUTSTANDING", 2)
        issue(c); issue(c)
        assert c.post("/api/invites", json={"amount_vsp": "1", "expires_days": 30, "permit": permit_for("1")}).status_code == 429
        assert chain.balances[ESCROW] == 4 * 10 ** 18  # the refused one escrowed nothing

    def test_permit_must_name_the_escrow(self, chain, db_and_client):
        _, c = db_and_client
        r = c.post("/api/invites", json={"amount_vsp": "1", "expires_days": 30, "permit": permit_for("1", spender="0x00000000000000000000000000000000000000ff")})
        assert r.status_code == 400


class TestRedeem:
    def test_peek_and_redeem(self, chain, db_and_client):
        _, c = db_and_client
        d = issue(c, "3")
        p = c.get(f"/api/invites/peek?code={d['code'].lower()}").json()
        assert p == {"status": "open", "amount_vsp": "3", "expires_at": d["expires_at"], "post_id": None}
        assert c.get("/api/invites/peek?code=VSP-AAAA-BBBB-CCCC").status_code == 404
        r = c.post("/api/invites/redeem", json={"code": d["code"].replace("-", ""), "address": NEWBIE})
        assert r.status_code == 200 and r.json()["amount_vsp"] == "3"
        assert chain.pays == [(NEWBIE, 3 * 10 ** 18)] and chain.balances[NEWBIE] == 3 * 10 ** 18
        assert c.get(f"/api/invites/peek?code={d['code']}").json()["status"] == "redeemed"
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": "0x00000000000000000000000000000000000000cd"}).status_code == 409
        lst = c.get(f"/api/invites?sponsor={sponsor_account().address}").json()["invites"][0]
        assert lst["status"] == "redeemed" and lst["redeemed_by"] == NEWBIE and lst["redeem_tx"].startswith("0x")

    def test_newcomers_only(self, chain, db_and_client):
        SessionLocal, c = db_and_client
        d = issue(c, "1")
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": sponsor_account().address}).status_code == 400
        chain.balances["0x00000000000000000000000000000000000000ee"] = 5
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": "0x00000000000000000000000000000000000000ee"}).status_code == 409
        with SessionLocal() as db:
            db.execute(text("INSERT INTO chain_user_stake VALUES ('0x00000000000000000000000000000000000000dd', 1, 0, 2.0)")); db.commit()
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": "0x00000000000000000000000000000000000000dd"}).status_code == 409
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": NEWBIE}).status_code == 200

    def test_payout_failure_reopens(self, chain, db_and_client):
        _, c = db_and_client
        d = issue(c, "1")
        chain.fail_pay = True
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": NEWBIE}).status_code == 502
        assert c.get(f"/api/invites/peek?code={d['code']}").json()["status"] == "open"
        chain.fail_pay = False
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": NEWBIE}).status_code == 200

    def test_expired(self, chain, db_and_client):
        SessionLocal, c = db_and_client
        d = issue(c, "1")
        with SessionLocal() as db:
            db.execute(text("UPDATE invite_code SET expires_at = :e"), {"e": datetime.now(timezone.utc) - timedelta(minutes=1)}); db.commit()
        assert c.get(f"/api/invites/peek?code={d['code']}").json()["status"] == "expired"
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": NEWBIE}).status_code == 409
        lst = c.get(f"/api/invites?sponsor={sponsor_account().address}").json()  # the list sweeps and refunds
        assert lst["invites"][0]["status"] == "expired" and lst["invites"][0]["refund_tx"]
        assert chain.pays == [(sponsor_account().address.lower(), 10 ** 18)] and chain.balances[ESCROW] == 0


class TestRevoke:
    def test_sponsor_signature_refunds(self, chain, db_and_client):
        from eth_account import Account
        from eth_account.messages import encode_defunct
        _, c = db_and_client
        d = issue(c, "4")
        ts = int(time.time())
        sig = Account.sign_message(encode_defunct(text=invites.revoke_message(d["code_hash"], ts)), SPONSOR_KEY).signature.hex()
        other = Account.sign_message(encode_defunct(text=invites.revoke_message(d["code_hash"], ts)), "0x" + "22" * 32).signature.hex()
        assert c.post("/api/invites/revoke", json={"code_hash": d["code_hash"], "ts": ts, "signature": other}).status_code == 403
        assert c.post("/api/invites/revoke", json={"code_hash": d["code_hash"], "ts": ts - 3600, "signature": sig}).status_code == 400
        r = c.post("/api/invites/revoke", json={"code_hash": d["code_hash"], "ts": ts, "signature": sig})
        assert r.status_code == 200 and r.json()["status"] == "revoked"
        assert chain.pays == [(sponsor_account().address.lower(), 4 * 10 ** 18)]
        assert c.post("/api/invites/revoke", json={"code_hash": d["code_hash"], "ts": ts, "signature": sig}).status_code == 409
        assert c.post("/api/invites/redeem", json={"code": d["code"], "address": NEWBIE}).status_code == 409


class TestClaimTie:
    def test_invitation_remembers_the_claim_it_was_shared_from(self, chain, db_and_client):  # patch_invites_2
        _, c = db_and_client
        d = issue(c, "1", post_id=116)
        assert d["post_id"] == 116
        assert c.get(f"/api/invites/peek?code={d['code']}").json()["post_id"] == 116
        rows = c.get(f"/api/invites?sponsor={sponsor_account().address}").json()["invites"]
        assert rows[0]["post_id"] == 116
        d2 = issue(c, "1")  # from Positions: no claim
        assert d2["post_id"] is None


class TestEscrow:
    def test_reconcile(self, chain, db_and_client):
        _, c = db_and_client
        issue(c, "2"); issue(c, "3")
        d = c.get("/api/invites/escrow").json()
        assert d == {"escrow_address": ESCROW, "open_vsp": "5", "escrow_balance_vsp": "5", "covered": True}
        chain.balances[ESCROW] = 10 ** 18
        assert c.get("/api/invites/escrow").json()["covered"] is False

    def test_wording_and_privacy(self, chain, db_and_client):
        """No person: the rows hold two addresses and amounts, nothing else; no IP, no contact, no name."""
        raw = (ROOT / "ops" / "compose" / "migrations" / "350_invite_code.sql").read_text().lower()
        ddl = "\n".join(l for l in raw.splitlines() if not l.startswith("--"))
        for banned in ("ip ", "email", "phone", "name ", "cookie", "user_agent"):
            assert banned not in ddl, banned
