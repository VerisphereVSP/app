# tests/test_claim_hide.py — patch_report_path
"""Operator takedown: hide/unhide flips chain_claim_text.admin_hidden, the cached hidden set follows (invalidated
on write), mask() substitutes the placeholder for hidden ids only, and the admin endpoints require the key and
write the audit log. SQLite in-memory with the migration's columns; the FastAPI functions called directly."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claim_hide  # noqa: E402


@pytest.fixture
def db():
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(sql_text(
            "CREATE TABLE chain_claim_text (post_id INTEGER PRIMARY KEY, claim_text TEXT, is_moderated BOOLEAN DEFAULT 0, "
            "admin_hidden BOOLEAN NOT NULL DEFAULT 0, admin_hidden_reason TEXT, admin_hidden_at TIMESTAMP)"))
        c.execute(sql_text("INSERT INTO chain_claim_text (post_id, claim_text) VALUES (1, 'the sky is blue'), (2, 'water is wet')"))
    s = sessionmaker(bind=eng)()
    claim_hide._cache = (0.0, frozenset())
    yield s
    s.close()


def test_mask_passes_clean_text(db):
    assert claim_hide.hidden_ids(db) == frozenset()
    assert claim_hide.mask("the sky is blue", 1, db) == "the sky is blue"
    assert claim_hide.mask("anything", None, db) == "anything"


def test_hide_then_unhide(db):
    assert claim_hide.set_hidden(db, 1, True, "doxxing")
    assert claim_hide.hidden_ids(db) == frozenset({1})
    assert claim_hide.mask("the sky is blue", 1, db) == claim_hide.HIDDEN_PLACEHOLDER
    assert claim_hide.mask("water is wet", 2, db) == "water is wet"
    row = db.execute(sql_text("SELECT admin_hidden, admin_hidden_reason, admin_hidden_at FROM chain_claim_text WHERE post_id=1")).fetchone()
    assert row[0] and row[1] == "doxxing" and row[2] is not None
    assert claim_hide.set_hidden(db, 1, False, None)
    assert claim_hide.hidden_ids(db) == frozenset()
    row = db.execute(sql_text("SELECT admin_hidden, admin_hidden_reason, admin_hidden_at FROM chain_claim_text WHERE post_id=1")).fetchone()
    assert not row[0] and row[1] is None and row[2] is None


def test_unknown_post_returns_false(db):
    assert claim_hide.set_hidden(db, 999, True, "x") is False


def test_cache_ttl_and_invalidate(db, monkeypatch):
    claim_hide.hidden_ids(db)  # warm, empty
    db.execute(sql_text("UPDATE chain_claim_text SET admin_hidden=1 WHERE post_id=2")); db.commit()
    assert claim_hide.hidden_ids(db) == frozenset(), "cached for the TTL"
    claim_hide.invalidate()
    assert claim_hide.hidden_ids(db) == frozenset({2})


def test_reason_is_truncated(db):
    claim_hide.set_hidden(db, 1, True, "r" * 2000)
    row = db.execute(sql_text("SELECT length(admin_hidden_reason) FROM chain_claim_text WHERE post_id=1")).fetchone()
    assert row[0] == 500


def test_display_filter_prefers_admin_hide(db, monkeypatch):
    """claim_views._moderate_text asks claim_hide first, then the content filter."""
    import importlib, types
    # claim_views imports web3 wiring at module import; stub the heavy bits it pulls in
    for name in ("mm_wallet",):
        if name not in sys.modules:
            sys.modules[name] = types.SimpleNamespace(w3=None)
    cv = importlib.import_module("claim_views")
    claim_hide.set_hidden(db, 1, True, "test")
    assert cv._moderate_text("the sky is blue", 1, db) == claim_hide.HIDDEN_PLACEHOLDER
    assert cv._moderate_text("the sky is blue", 2, db) == "the sky is blue"
    assert cv._moderate_text("the sky is blue") == "the sky is blue"


def test_admin_endpoints_wired():
    """main.py: the three routes exist, call require_admin, and audit hide/unhide."""
    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
    for route in ('"/api/admin/claims/{post_id}/hide"', '"/api/admin/claims/{post_id}/unhide"', '"/api/admin/claims/hidden"'):
        assert route in src, route
    hide = src[src.index("def admin_hide_claim"):src.index("def admin_unhide_claim")]
    unhide = src[src.index("def admin_unhide_claim"):src.index("def admin_hidden_claims")]
    assert 'require_admin(request, db=db, action="hide_claim"' in hide
    assert 'require_admin(request, db=db, action="unhide_claim"' in unhide
    assert "set_hidden(db, post_id, True" in hide and "set_hidden(db, post_id, False" in unhide
