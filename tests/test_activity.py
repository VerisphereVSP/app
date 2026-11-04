# app/tests/test_activity.py — patch_activity
"""What happened on your claims: computed on read from the chain tables and invite_code, aggregated per claim, with the
cursor held by the client. Runs against sqlite with the migrations' DDL as written (types mapped as the invites suite
does)."""
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

import activity  # noqa: E402
from db import get_db  # noqa: E402

ME = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
THIRD = "0x" + "c" * 40
NOW = int(time.time())


def ddl():
    raw = ""
    for m in ("030_chain_state.sql", "220_chain_tx.sql", "350_invite_code.sql", "351_invite_code_post.sql", "370_chain_tx_post_idx.sql"):
        raw += (ROOT / "ops" / "compose" / "migrations" / m).read_text() + "\n"
    d = "\n".join(l for l in raw.splitlines() if not l.startswith("--")).replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
    d = d.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    d = d.replace("TIMESTAMPTZ NOT NULL DEFAULT now()", "TEXT NOT NULL DEFAULT (datetime('now'))")
    d = d.replace("TIMESTAMPTZ", "TEXT").replace("NUMERIC(78,0)", "TEXT").replace("DOUBLE PRECISION", "REAL")
    return d


@pytest.fixture
def harness():
    eng = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        for stmt in ddl().split(";"):
            if stmt.strip():
                c.execute(text(stmt))
    SessionLocal = sessionmaker(bind=eng, future=True)
    app = FastAPI()
    app.include_router(activity.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _db
    return SessionLocal, TestClient(app)


def seed(S, *, post=142, text_="Regular coffee consumption is associated with a lower risk of stroke.", support=100.0, challenge=60.0, creator=THIRD):
    with S() as db:
        db.execute(text("INSERT INTO chain_post (post_id, content_type, creator, support_total, challenge_total, is_active) VALUES (:p, 0, :c, :s, :ch, 1)"),
                   {"p": post, "c": creator, "s": support, "ch": challenge})
        db.execute(text("INSERT INTO chain_claim_text (post_id, claim_text) VALUES (:p, :t)"), {"p": post, "t": text_})
        db.commit()


def stake(S, who, post, side, amount, *, tx_id=None, epoch=None, action="stake"):
    with S() as db:
        db.execute(text("INSERT INTO chain_tx (id, block_number, tx_hash, log_index, block_epoch, contract, event_name, action_type, user_address, post_id, amount_vsp, is_challenge) "
                        "VALUES (:id, 1, :h, 0, :e, 'SE', :en, :at, :u, :p, :a, :ic)"),
                   {"id": tx_id, "h": f"0x{who[-4:]}{post}{tx_id or ''}{action}", "e": epoch or NOW, "en": "StakeAdded" if action == "stake" else "StakeWithdrawn",
                    "at": action, "u": who, "p": post, "a": amount, "ic": 1 if side == "challenge" else 0})
        db.commit()


def position(S, who, post, side, amount):
    with S() as db:
        db.execute(text("INSERT INTO chain_user_stake (user_address, post_id, side, amount) VALUES (:u, :p, :s, :a)"),
                   {"u": who, "p": post, "s": 1 if side == "challenge" else 0, "a": amount})
        db.commit()


class TestFeed:
    def test_nothing_held_nothing_to_tell(self, harness):
        S, c = harness
        seed(S)
        stake(S, OTHER, 142, "challenge", 10)
        r = c.get(f"/api/activity/{ME}")
        assert r.status_code == 200
        j = r.json()
        assert j["claims"] == [] and j["invites"] == [] and j["count"] == 0
        assert j["cursor"] == 1  # the client stores this even when there is nothing yet

    def test_others_moves_on_my_claim_are_aggregated_with_the_score_before_and_after(self, harness):
        S, c = harness
        seed(S, support=100.0, challenge=160.0)  # totals NOW, after the moves below
        position(S, ME, 142, "support", 25)
        stake(S, ME, 142, "support", 25, tx_id=1)            # my own move is not news
        stake(S, OTHER, 142, "challenge", 70, tx_id=2)
        stake(S, THIRD, 142, "challenge", 30, tx_id=3)
        stake(S, OTHER, 142, "support", 5, tx_id=4)
        j = c.get(f"/api/activity/{ME}").json()
        assert j["count"] == 1 and len(j["claims"]) == 1
        g = j["claims"][0]
        assert g["post_id"] == 142 and g["role"] == "support" and g["your_vsp"] == 25
        assert g["challenge_n"] == 2 and g["challenge_vsp"] == 100 and g["support_n"] == 1 and g["support_vsp"] == 5
        assert g["wallets_n"] == 2
        # before the others moved: support 95, challenge 60 → 61.3 %; now 100 / 160 → 38.5 %: my side lost the majority
        assert g["score_before"] == 61.3 and g["score_now"] == 38.5 and g["flipped"] is True
        assert g["text"].startswith("Regular coffee") and g["last_id"] == 4
        assert j["cursor"] == 4

    def test_the_cursor_hides_what_was_already_seen(self, harness):
        S, c = harness
        seed(S)
        position(S, ME, 142, "challenge", 10)
        stake(S, OTHER, 142, "support", 5, tx_id=1)
        first = c.get(f"/api/activity/{ME}").json()
        assert first["count"] == 1
        again = c.get(f"/api/activity/{ME}", params={"since": first["cursor"]}).json()
        assert again["count"] == 0 and again["claims"] == []
        stake(S, OTHER, 142, "support", 5, tx_id=2)
        later = c.get(f"/api/activity/{ME}", params={"since": first["cursor"]}).json()
        assert later["count"] == 1 and later["claims"][0]["support_n"] == 1 and later["cursor"] == 2

    def test_creator_hears_about_a_claim_they_hold_nothing_on(self, harness):
        S, c = harness
        seed(S, creator=ME, support=0.0, challenge=12.0)
        stake(S, OTHER, 142, "challenge", 12, tx_id=1)
        g = c.get(f"/api/activity/{ME}").json()["claims"][0]
        assert g["role"] == "creator" and g["your_vsp"] == 0 and g["challenge_n"] == 1
        assert g["score_before"] is None and g["score_now"] == 0.0 and g["flipped"] is False

    def test_withdrawals_and_links_count_too(self, harness):
        S, c = harness
        seed(S, support=50.0, challenge=0.0)
        position(S, ME, 142, "support", 50)
        stake(S, OTHER, 142, "challenge", 20, tx_id=1, action="unstake")
        with S() as db:
            db.execute(text("INSERT INTO chain_post (post_id, content_type, creator, support_total, challenge_total, is_active) VALUES (900, 1, :c, 0, 0, 1)"), {"c": OTHER})
            db.execute(text("INSERT INTO chain_link (link_post_id, from_post_id, to_post_id, is_challenge) VALUES (900, 901, 142, 1)"))
            db.execute(text("INSERT INTO chain_tx (id, block_number, tx_hash, log_index, block_epoch, contract, event_name, action_type, user_address, post_id, amount_vsp, is_challenge) "
                            "VALUES (2, 1, '0xlink', 0, :e, 'LG', 'EdgeAdded', 'link', :u, 900, NULL, 1)"), {"e": NOW, "u": OTHER})
            db.commit()
        g = c.get(f"/api/activity/{ME}").json()["claims"][0]
        assert g["post_id"] == 142 and g["unstake_n"] == 1 and g["unstake_vsp"] == 20
        assert g["links_n"] == 1 and g["links_challenge_n"] == 1
        # the withdrawal was on the challenge side: before it, challenge stood at 20 → 71.4 %; now 100 %
        assert g["score_before"] == 71.4 and g["score_now"] == 100.0

    def test_first_look_reaches_back_a_week_only(self, harness):
        S, c = harness
        seed(S)
        position(S, ME, 142, "support", 1)
        stake(S, OTHER, 142, "support", 5, tx_id=1, epoch=NOW - 30 * 86400)
        stake(S, OTHER, 142, "support", 5, tx_id=2, epoch=NOW - 86400)
        g = c.get(f"/api/activity/{ME}").json()["claims"][0]
        assert g["support_n"] == 1
        # with a cursor, age no longer matters
        g = c.get(f"/api/activity/{ME}", params={"since": 0}).json()["claims"][0]
        assert g["support_n"] == 2

    def test_newest_claim_first_and_limit(self, harness):
        S, c = harness
        for p, when in ((1, NOW - 300), (2, NOW - 100), (3, NOW - 200)):
            seed(S, post=p, text_=f"claim {p}")
            position(S, ME, p, "support", 1)
            stake(S, OTHER, p, "challenge", 1, tx_id=p, epoch=when)
        j = c.get(f"/api/activity/{ME}", params={"limit": 2}).json()
        assert [g["post_id"] for g in j["claims"]] == [2, 3] and j["count"] == 3

    def test_sponsor_sees_redeemed_invitations(self, harness):
        S, c = harness
        with S() as db:
            db.execute(text("INSERT INTO invite_code (code_hash, sponsor, amount_wei, expires_at, status, redeemed_by, redeemed_at, post_id) "
                            "VALUES ('h1', :me, '2000000000000000000', '2030-01-01', 'redeemed', :o, :r, 142)"),
                       {"me": ME, "o": OTHER, "r": (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")})
            db.execute(text("INSERT INTO invite_code (code_hash, sponsor, amount_wei, expires_at, status) VALUES ('h2', :me, '1000000000000000000', '2030-01-01', 'open')"), {"me": ME})
            db.commit()
        j = c.get(f"/api/activity/{ME}").json()
        assert j["count"] == 1 and j["claims"] == []
        assert j["invites"] == [{"id": 1, "amount_vsp": 2.0, "post_id": 142, "redeemed_at": j["invites"][0]["redeemed_at"]}]
        assert "redeemed_by" not in j["invites"][0]  # who redeemed is not the sponsor's business here
        # once looked at, gone
        assert c.get(f"/api/activity/{ME}", params={"since": j["cursor"], "since_ts": j["cursor_ts"]}).json()["count"] == 0

    def test_count_is_cheap_and_matches(self, harness):
        S, c = harness
        seed(S)
        position(S, ME, 142, "support", 1)
        stake(S, OTHER, 142, "challenge", 1, tx_id=1)
        r = c.get(f"/api/activity/{ME}/count").json()
        assert r == {"count": 1, "cursor": 1, "cursor_ts": r["cursor_ts"]}

    def test_bad_inputs(self, harness):
        S, c = harness
        assert c.get("/api/activity/nope").status_code == 400
        assert c.get(f"/api/activity/{ME}", params={"since_ts": "yesterday"}).status_code == 400
        assert c.get(f"/api/activity/{ME}", params={"limit": 0}).status_code == 422

    def test_address_case_does_not_matter(self, harness):
        S, c = harness
        seed(S)
        position(S, ME, 142, "support", 1)
        stake(S, OTHER, 142, "challenge", 1, tx_id=1)
        assert c.get(f"/api/activity/{ME.upper().replace('0X', '0x')}").json()["count"] == 1
