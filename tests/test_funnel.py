# app/tests/test_funnel.py — patch_funnel
"""The share-to-stake funnel: daily counts per step and nothing else. The routes run against sqlite with the migration's
DDL as written; the share page and opinion hooks are exercised through the snapshot router mounted alongside."""
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import snapshot  # noqa: E402
from db import get_db  # noqa: E402

POSTS = {142: {"post_id": 142, "content_type": 0, "support_total": 4210.0, "challenge_total": 1630.0, "effective_vs": 44.0, "base_vs": 40.0, "is_active": True}}


@pytest.fixture(autouse=True)
def stub_reads(monkeypatch):
    fake_db = types.ModuleType("chain.chain_db")
    fake_db.get_post_info = lambda db, pid: POSTS.get(pid)
    fake_db.get_claim_text = lambda db, pid: "Regular coffee consumption is associated with a lower risk of stroke."
    monkeypatch.setitem(sys.modules, "chain.chain_db", fake_db)
    fake_views = types.ModuleType("claim_views")
    fake_views._moderate_text = lambda text, post_id=None, db=None: text
    monkeypatch.setitem(sys.modules, "claim_views", fake_views)
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)


@pytest.fixture
def harness():
    eng = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        for m in ("340_claim_opinion_daily.sql", "360_funnel_daily.sql"):
            ddl = (ROOT / "ops" / "compose" / "migrations" / m).read_text()
            c.execute(text("\n".join(l for l in ddl.splitlines() if not l.startswith("--"))))
    SessionLocal = sessionmaker(bind=eng, future=True)
    app = FastAPI()
    app.include_router(snapshot.router)
    app.include_router(funnel.router)
    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _db
    return SessionLocal, TestClient(app)


def rows(SessionLocal):
    with SessionLocal() as db:
        return {r[0]: r[1] for r in db.execute(text("SELECT step, n FROM funnel_daily")).fetchall()}


class TestCounts:
    def test_browser_steps_count_and_the_rest_are_refused(self, harness):
        S, c = harness
        for step in ("start_open", "start_open", "start_wallet", "start_skip", "back_tap"):
            assert c.post("/api/funnel", json={"step": step}).status_code == 204
        assert rows(S) == {"start_open": 2, "start_wallet": 1, "start_skip": 1, "back_tap": 1}
        for bad in ("share_view", "invite_redeemed", "side_taken", "nonsense", ""):  # server-side steps cannot be posted
            assert c.post("/api/funnel", json={"step": bad}).status_code == 400
        assert rows(S) == {"start_open": 2, "start_wallet": 1, "start_skip": 1, "back_tap": 1}

    def test_share_page_counts_people_and_previews_apart(self, harness):
        S, c = harness
        assert c.get("/c/142").status_code == 200
        assert c.get("/c/142", headers={"user-agent": "Mozilla/5.0 (iPhone) Safari"}).status_code == 200
        assert c.get("/c/142", headers={"user-agent": "WhatsApp/2.23.20.0 A"}).status_code == 200
        assert c.get("/c/142", headers={"user-agent": "facebookexternalhit/1.1"}).status_code == 200
        assert c.head("/c/142").status_code == 200  # a HEAD (link checkers) is not a view
        assert c.get("/c/999").status_code == 404   # an unknown claim counts nothing
        assert rows(S) == {"share_view": 2, "share_preview": 2}

    def test_a_first_side_counts_but_a_change_of_mind_does_not(self, harness):
        S, c = harness
        assert c.post("/api/claims/142/opinion", json={"side": "support"}).status_code == 200
        assert c.post("/api/claims/142/opinion", json={"side": "challenge", "from_side": "support"}).status_code == 200
        assert c.post("/api/claims/142/opinion", json={"side": "nope"}).status_code == 400
        assert rows(S) == {"side_taken": 1}

    def test_only_day_step_n_is_stored(self, harness):
        S, c = harness
        c.post("/api/funnel", json={"step": "start_staked"}, headers={"user-agent": "Mozilla/5.0", "x-forwarded-for": "203.0.113.9"})
        with S() as db:
            cols = [r[1] for r in db.execute(text("PRAGMA table_info(funnel_daily)")).fetchall()]
            assert cols == ["day", "step", "n"]
            assert db.execute(text("SELECT count(*) FROM funnel_daily")).scalar() == 1
        ddl = (ROOT / "ops" / "compose" / "migrations" / "360_funnel_daily.sql").read_text().lower()
        for forbidden in ("address", "wallet", "ip", "agent", "cookie", "visitor"):
            assert forbidden not in ddl.replace("-- ", "").split("create table")[1]


class TestReport:
    def test_report_shape_and_conversions(self, harness):
        S, c = harness
        c.get("/c/142"); c.get("/c/142"); c.get("/c/142"); c.get("/c/142")        # 4 views
        c.post("/api/claims/142/opinion", json={"side": "support"})              # 1 side
        c.post("/api/claims/142/opinion", json={"side": "challenge"})            # 2 sides
        for step in ("back_tap", "start_open", "start_wallet", "start_funded", "start_staked"):
            c.post("/api/funnel", json={"step": step})
        d = c.get("/api/funnel?days=7").json()
        assert d["days"] == 7 and d["steps"] == list(funnel.STEPS)
        assert d["totals"]["share_view"] == 4 and d["totals"]["side_taken"] == 2 and d["totals"]["start_staked"] == 1
        assert d["totals"]["share_preview"] == 0 and d["totals"]["invite_redeemed"] == 0  # every step present, zero or not
        assert len(d["by_day"]) == 1 and d["by_day"][0]["share_view"] == 4 and d["by_day"][0]["start_skip"] == 0
        assert d["conversion"] == {"view_to_side": 0.5, "side_to_back": 0.5, "back_to_open": 1.0, "open_to_wallet": 1.0,
                                   "wallet_to_funded": 1.0, "funded_to_staked": 1.0, "view_to_staked": 0.25}
        assert c.get("/api/funnel?days=0").status_code == 422 and c.get("/api/funnel?days=91").status_code == 422

    def test_empty_report_has_null_conversions(self, harness):
        _, c = harness
        d = c.get("/api/funnel").json()
        assert d["days"] == 14 and d["by_day"] == [] and all(v == 0 for v in d["totals"].values())
        assert all(v is None for v in d["conversion"].values())


class TestWiring:
    def test_preview_bots(self):
        assert funnel.is_preview_bot("WhatsApp/2.23.20.0 A") and funnel.is_preview_bot("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)")
        assert funnel.is_preview_bot("TelegramBot (like TwitterBot)") and not funnel.is_preview_bot("Mozilla/5.0 (Windows NT 10.0) Chrome/130")
        assert not funnel.is_preview_bot(None)

    def test_share_script_counts_the_button_once(self):
        js = (ROOT / "assets" / "snapshot.js").read_text()
        assert js.count('step: "back_tap"') == 1 and 'navigator.sendBeacon("/api/funnel"' in js and 'back.addEventListener("click", countBack)' in js

    def test_rate_limit_and_registration(self):
        limits = json.loads((ROOT / "ops" / "rate_limits.json").read_text())["endpoints"]
        assert limits["/api/funnel"] == 60
        main = (ROOT / "main.py").read_text()
        assert "from funnel import router as funnel_router" in main and "app.include_router(funnel_router)" in main

    def test_a_failed_count_never_raises(self):
        class Boom:
            def execute(self, *a, **k): raise RuntimeError("db down")
            def rollback(self): raise RuntimeError("and again")
        funnel.count(Boom(), "start_open")  # logs, returns
        funnel.count(None, "share_view")    # even without a session
