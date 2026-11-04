# app/tests/test_article_orphans.py — patch_article_orphans
"""A sentence whose text is an on-chain claim is never shown as prose: after the indexer's reset, an orphan is attached
(same topic or relevant) or hidden (belongs elsewhere). sqlite, no embeddings: relevance is monkeypatched."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # the embedding client is not needed here; the container has it, a bare checkout may not
    import openai  # noqa: F401
except ImportError:  # pragma: no cover
    import types
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=object)
from articles import claim_indexer as ci  # noqa: E402

DDL = """
CREATE TABLE topic_article (article_id INTEGER PRIMARY KEY, topic_key TEXT, title TEXT, cached_response TEXT, last_refreshed_at TEXT);
CREATE TABLE article_section (section_id INTEGER PRIMARY KEY, article_id INTEGER, heading TEXT, sort_order INTEGER);
CREATE TABLE article_sentence (sentence_id INTEGER PRIMARY KEY, section_id INTEGER, text TEXT, post_id INTEGER, is_hidden INTEGER NOT NULL DEFAULT 0, sort_order INTEGER);
CREATE TABLE chain_claim_text (post_id INTEGER PRIMARY KEY, claim_text TEXT);
CREATE TABLE claim (claim_id INTEGER PRIMARY KEY, claim_text TEXT, post_id INTEGER, topic TEXT);
"""
COMMUNISM = "Replacing capitalism with communism raises a society's standard of living above what a society would otherwise have reached."
MILANKOVITCH = "Milankovitch cycles are the primary driver of all climate change."


@pytest.fixture
def db():
    eng = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        for stmt in DDL.split(";"):
            if stmt.strip():
                c.execute(text(stmt))
    S = sessionmaker(bind=eng, future=True)
    s = S()
    s.execute(text("INSERT INTO topic_article VALUES (1, 'climate change', 'Climate Change', 'cached', 'x'), (2, 'economic systems', 'Economic Systems', 'cached', 'x')"))
    s.execute(text("INSERT INTO article_section VALUES (10, 1, 'Mitigation', 0), (20, 2, 'Overview', 0)"))
    s.execute(text("INSERT INTO chain_claim_text VALUES (13, :c), (7, :m)"), {"c": COMMUNISM, "m": MILANKOVITCH})
    s.execute(text("INSERT INTO claim VALUES (170, :c, 13, 'Economic Systems'), (171, :m, 7, 'Climate Change')"), {"c": COMMUNISM, "m": MILANKOVITCH})
    s.commit()
    yield s
    s.close()


def sent(db, sid, sec, t, post_id=None, hidden=0):
    db.execute(text("INSERT INTO article_sentence VALUES (:i, :s, :t, :p, :h, :i)"), {"i": sid, "s": sec, "t": t, "p": post_id, "h": hidden})
    db.commit()


def row(db, sid):
    return db.execute(text("SELECT post_id, is_hidden FROM article_sentence WHERE sentence_id = :s"), {"s": sid}).fetchone()


class TestSettle:
    def test_claim_of_another_topic_is_hidden_here_and_carried_there(self, db, monkeypatch):
        monkeypatch.setattr(ci, "is_claim_relevant_to_article", lambda *a, **k: False)
        sent(db, 4478, 10, COMMUNISM)                 # the prod case: typed in Climate Change, orphaned by the reset
        sent(db, 4506, 20, COMMUNISM, post_id=13)     # its proper home already carries it
        assert ci.settle_orphan_claim_sentences(db, 1) == {"attached": 0, "hidden": 1}
        assert row(db, 4478) == (None, 1)
        assert row(db, 4506) == (13, 0)

    def test_claim_of_this_topic_is_attached_whatever_the_relevance_says(self, db, monkeypatch):
        monkeypatch.setattr(ci, "is_claim_relevant_to_article", lambda *a, **k: False)
        sent(db, 1, 10, "  milankovitch cycles are the primary   driver of all climate change. ")  # whitespace/case differ
        assert ci.settle_orphan_claim_sentences(db, 1) == {"attached": 1, "hidden": 0}
        assert row(db, 1) == (7, 0)

    def test_relevant_claim_of_another_topic_is_attached(self, db, monkeypatch):
        monkeypatch.setattr(ci, "is_claim_relevant_to_article", lambda *a, **k: True)
        sent(db, 1, 10, COMMUNISM)
        assert ci.settle_orphan_claim_sentences(db, 1) == {"attached": 1, "hidden": 0}
        assert row(db, 1) == (13, 0)

    def test_already_carried_claim_is_not_attached_twice(self, db, monkeypatch):
        monkeypatch.setattr(ci, "is_claim_relevant_to_article", lambda *a, **k: True)
        sent(db, 1, 10, MILANKOVITCH, post_id=7)
        sent(db, 2, 10, MILANKOVITCH)
        assert ci.settle_orphan_claim_sentences(db, 1) == {"attached": 0, "hidden": 1}
        assert row(db, 1) == (7, 0) and row(db, 2) == (None, 1)

    def test_prose_and_hidden_and_carriers_are_left_alone(self, db, monkeypatch):
        monkeypatch.setattr(ci, "is_claim_relevant_to_article", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not called")))
        sent(db, 1, 10, "Transitioning to renewable energy sources can reduce emissions.")
        sent(db, 2, 10, COMMUNISM, hidden=1)
        sent(db, 3, 10, MILANKOVITCH, post_id=7)
        assert ci.settle_orphan_claim_sentences(db, 1) == {"attached": 0, "hidden": 0}
        assert row(db, 1) == (None, 0) and row(db, 2) == (None, 1) and row(db, 3) == (7, 0)

    def test_unknown_article(self, db):
        assert ci.settle_orphan_claim_sentences(db, 99) == {"attached": 0, "hidden": 0}


class TestRepair:
    def test_repair_touches_only_changed_articles_and_clears_their_cache(self, db, monkeypatch):
        monkeypatch.setattr(ci, "is_claim_relevant_to_article", lambda *a, **k: False)
        sent(db, 4478, 10, COMMUNISM)
        sent(db, 4506, 20, COMMUNISM, post_id=13)
        out = ci.repair_orphan_claim_sentences(db)
        assert out == {"climate change": {"attached": 0, "hidden": 1}}
        caches = dict(db.execute(text("SELECT topic_key, cached_response FROM topic_article")).fetchall())
        assert caches == {"climate change": None, "economic systems": "cached"}
        # idempotent
        assert ci.repair_orphan_claim_sentences(db) == {}
