# app/tests/test_snapshot.py — patch_verisnap_share
"""
Verisphere Snapshot: the snapshot JSON, the PNG card and the /c/<id> share page. Pure unit: the indexed reads are stubbed,
the router is mounted on a bare FastAPI app with the DB dependency overridden; no chain, no Postgres.
"""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import snapshot  # noqa: E402
from db import get_db  # noqa: E402

POSTS = {
    142: {"post_id": 142, "content_type": 0, "support_total": 4210.0, "challenge_total": 1630.0,
          "effective_vs": 44.0, "base_vs": 40.0, "is_active": True},
    17: {"post_id": 17, "content_type": 0, "support_total": 0.6, "challenge_total": 2.4,
         "effective_vs": -73.0, "base_vs": -70.0, "is_active": True},
    9: {"post_id": 9, "content_type": 1, "support_total": 1.0, "challenge_total": 0.0, "effective_vs": 10.0, "base_vs": 10.0, "is_active": True},
    5: {"post_id": 5, "content_type": 0, "support_total": 0.0, "challenge_total": 0.0, "effective_vs": 0.0, "base_vs": 0.0, "is_active": True},
}
TEXTS = {142: "Regular coffee consumption is associated with a lower risk of stroke.",
         17: "The 1986 FIFA World Cup final went to extra time.", 5: "Nobody staked here yet."}


@pytest.fixture(autouse=True)
def stub_reads(monkeypatch):
    fake_db = types.ModuleType("chain.chain_db")
    fake_db.get_post_info = lambda db, pid: POSTS.get(pid)
    fake_db.get_claim_text = lambda db, pid: TEXTS.get(pid)
    monkeypatch.setitem(sys.modules, "chain.chain_db", fake_db)
    fake_views = types.ModuleType("claim_views")
    fake_views._moderate_text = lambda text, post_id=None, db=None: text
    monkeypatch.setitem(sys.modules, "claim_views", fake_views)
    snapshot._card_cache.clear()
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(snapshot.router)
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


class TestPureHelpers:
    def test_bands_on_the_support_axis(self):
        assert snapshot.band_for(75) == ("Strongly supported", "support")
        assert snapshot.band_for(44) == ("Mostly supported", "support")
        assert snapshot.band_for(0) == ("Contested", "neutral")
        assert snapshot.band_for(-19.9) == ("Contested", "neutral")
        assert snapshot.band_for(-40) == ("Mostly challenged", "challenge")
        assert snapshot.band_for(-73) == ("Strongly challenged", "challenge")

    def test_support_pct(self):
        assert snapshot.support_pct(4210, 1630) == 72
        assert snapshot.support_pct(0, 0) is None
        assert snapshot.support_pct(1, 0) == 100

    def test_next_settlement_is_next_midnight_utc(self):
        now = datetime(2026, 10, 26, 20, 48, tzinfo=timezone.utc)
        assert snapshot.next_settlement(now) == datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)


class TestSnapshotJson:
    def test_fields(self, client):
        r = client.get("/api/claims/142/snapshot", headers={"x-forwarded-proto": "https", "x-forwarded-host": "app.verisphere.co"})
        assert r.status_code == 200
        d = r.json()
        assert d["band"] == "Mostly supported" and d["support_pct"] == 72 and d["verity_score"] == 44.0
        assert d["total_stake"] == 5840.0 and d["stake_line"] == "72 % of stake backs this claim"
        assert d["share_url"] == "https://app.verisphere.co/c/142"
        assert d["card_url"] == "https://app.verisphere.co/api/claims/142/card.png"
        assert d["app_url"] == "https://app.verisphere.co/claims?post=142"
        # patch_snapshot_invite: the text form leads with the invitation question, quotes the claim, states the
        # standing, and ends with the call + link
        assert d["share_text"] == ("Think it's wrong? “Regular coffee consumption is associated with a lower risk of stroke.” — "
                                   "72 % of stake on Verisphere backs it (Mostly supported, VS +44). Challenge it: https://app.verisphere.co/c/142")
        assert d["invite_question"] == "Think it's wrong?" and d["invite_call"] == "Challenge it"
        assert d["invite_line"] == "Think it's wrong? Challenge it at app.verisphere.co/c/142"
        assert d["next_settlement_at"].endswith("T00:00:00Z")

    def test_challenged_claim_reads_from_the_challenge_side(self, client):
        d = client.get("/api/claims/17/snapshot").json()
        assert d["band"] == "Strongly challenged" and d["stake_line"] == "80 % of stake challenges this claim"
        assert d["invite_question"] == "Think it's right?" and d["invite_call"] == "Support it"
        assert "80 % of stake on Verisphere challenges it (Strongly challenged, VS -73). Support it: " in d["share_text"]

    def test_no_stake_and_no_verdict_words(self, client):
        d = client.get("/api/claims/5/snapshot").json()
        assert d["stake_line"] == "No stake yet" and d["band"] == "Contested"
        assert d["invite_question"] == "Nobody has taken a side yet." and d["invite_call"] == "Be the first"
        assert "no stake on Verisphere so far (Contested, VS +0). Be the first: " in d["share_text"]
        for pid in (5, 17, 142):
            d = client.get(f"/api/claims/{pid}/snapshot").json()
            words = " ".join(str(d[k]) for k in ("band", "stake_line", "share_text", "invite_line")).lower()
            for banned in ("true", "false", "price", "value", "invest"):
                assert banned not in words, (pid, banned)

    def test_contested_with_stake_asks_which_side(self):
        assert snapshot.invite_for("neutral", 55) == ("Which side are you on?", "Take a side")

    def test_links_and_unknown_are_404(self, client):
        assert client.get("/api/claims/9/snapshot").status_code == 404
        assert client.get("/api/claims/999/snapshot").status_code == 404

    def test_public_app_url_overrides_forwarded_host(self, client, monkeypatch):
        monkeypatch.setenv("PUBLIC_APP_URL", "https://example.test/")
        assert client.get("/api/claims/142/snapshot").json()["share_url"] == "https://example.test/c/142"


class TestCard:
    def test_png_1200x630_and_cached(self, client, monkeypatch):
        from PIL import Image
        import io
        r = client.get("/api/claims/142/card.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        im = Image.open(io.BytesIO(r.content))
        assert im.size == (1200, 630)
        calls = []
        monkeypatch.setattr(snapshot, "render_card", lambda snap: calls.append(1) or b"x")
        r2 = client.get("/api/claims/142/card.png")
        assert r2.content == r.content and calls == []  # served from the 60 s cache

    def test_fit_keeps_44px_for_short_and_shrinks_long_without_cutting(self):
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        avail = (630 - 92) - 24 - 84 - 36 - 140
        short = "\u201cRegular coffee consumption is associated with a lower risk of stroke.\u201d"
        f, lines, _ = snapshot.fit_claim(d, short, 1088, avail)
        assert f.size == 44 and len(lines) == 2
        long = "\u201c" + "The modern-day rise in global temperatures is driven by human activities, especially fossil fuel burning since the Industrial Revolution, which has increased greenhouse gases in the atmosphere. " * 2 + "\u201d"
        f, lines, _ = snapshot.fit_claim(d, long, 1088, avail)
        assert f.size < 44 and not lines[-1].endswith("\u2026") and lines[-1].endswith("\u201d")
        huge = "\u201c" + "word " * 400 + "\u201d"
        f, lines, _ = snapshot.fit_claim(d, huge, 1088, avail)
        assert f.size == 26 and lines[-1].endswith("\u2026")

    def test_footer_invitation_is_drawn_per_standing(self):
        """patch_snapshot_invite: the footer band differs between a supported and a challenged claim (different
        invitation) and carries violet (the call + url) in every case."""
        from PIL import Image
        import io
        def footer(pid):
            png = snapshot.render_card(snapshot.build_snapshot(None, pid, "https://app.verisphere.co"))
            im = Image.open(io.BytesIO(png)).convert("RGB")
            return im.crop((56, 630 - 40 - 28, 700, 630 - 40))
        a, b, c = footer(142), footer(17), footer(5)
        assert a.tobytes() != b.tobytes() and b.tobytes() != c.tobytes()
        for im in (a, b, c):
            assert any(col == (0x37, 0x29, 0x9B) for _, col in im.getcolors(1 << 20)), "no violet call/url in the footer"

    def test_long_text_is_wrapped_and_truncated(self):
        snap = snapshot.build_snapshot(None, 142, "https://x")
        snap["text"] = "word " * 120
        png = snapshot.render_card(snap)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


class TestSharePage:
    def test_open_graph_meta_and_no_scripts(self, client):
        r = client.get("/c/142", headers={"x-forwarded-proto": "https", "x-forwarded-host": "app.verisphere.co"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        h = r.text
        assert '<meta property="og:image" content="https://app.verisphere.co/api/claims/142/card.png">' in h
        assert '<meta name="twitter:card" content="summary_large_image">' in h
        assert 'og:title" content="Regular coffee consumption' in h
        assert 'href="https://app.verisphere.co/claims?post=142"' in h
        assert "<script" not in h  # the edge CSP allows no inline script; the page needs none
        assert "Verisnap" not in h and "Verisphere Snapshot of claim #142" in h  # patch_snapshot_naming
        assert "<h2>Think it&#x27;s wrong?</h2>" in h and ">Challenge it on Verisphere</a>" in h  # patch_snapshot_invite
        assert ">Support it on Verisphere</a>" in client.get("/c/17").text
        assert client.head("/c/142").status_code == 200 and client.head("/api/claims/142/card.png").status_code == 200

    def test_share_page_escapes_text(self, client):
        TEXTS[142] = 'Says <b>"hi"</b> & bye'
        try:
            h = client.get("/c/142").text
            assert "<b>" not in h and "&lt;b&gt;" in h and "&amp;" in h
        finally:
            TEXTS[142] = "Regular coffee consumption is associated with a lower risk of stroke."
