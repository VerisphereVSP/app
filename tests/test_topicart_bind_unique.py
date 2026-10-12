# tests/test_topicart_bind_unique.py — patch_topicart_bind_unique
"""Orphan binding under the one-article-per-topic constraint (migration 311). Before the patch the claim-linkage
UPDATE bound an orphan to a topic that already had an article, the UNIQUE constraint fired, and the whole hourly
reconcile aborted (prod, every hour). Now: free topics are bound, taken topics are reported as contested, the pass
completes. SQLite with the same UNIQUE constraint; the centroid step is stubbed out (pgvector)."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import articles.topic_article_canonical as tac  # noqa: E402

SCHEMA = """
CREATE TABLE topic (topic_id INTEGER PRIMARY KEY, label TEXT, centroid TEXT);
CREATE TABLE topic_article (article_id INTEGER PRIMARY KEY, topic_key TEXT, title TEXT, topic_id INTEGER,
                            CONSTRAINT uq_topic_article_topic_id UNIQUE (topic_id));
CREATE TABLE article_section (section_id INTEGER PRIMARY KEY, article_id INTEGER, sort_order INTEGER);
CREATE TABLE article_sentence (sentence_id INTEGER PRIMARY KEY, section_id INTEGER, post_id INTEGER, sort_order INTEGER);
CREATE TABLE chain_claim_text (post_id INTEGER PRIMARY KEY, claim_text TEXT);
CREATE TABLE claim (claim_id INTEGER PRIMARY KEY, claim_text TEXT);
CREATE TABLE claim_topic (claim_id INTEGER, topic_id INTEGER);
"""


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                c.execute(text(stmt))
        # topic 1 already has article 1 (canonical); topic 2 is free
        c.execute(text("INSERT INTO topic VALUES (1,'governance',NULL),(2,'climate',NULL)"))
        c.execute(text("INSERT INTO topic_article VALUES (1,'governance','Governance',1),(2,'gov-dupe','Governance again',NULL),(3,'climate','Climate',NULL)"))
        # article 2's claims link to topic 1 (taken); article 3's to topic 2 (free)
        c.execute(text("INSERT INTO article_section VALUES (20,2,0),(30,3,0)"))
        c.execute(text("INSERT INTO article_sentence VALUES (200,20,6,0),(300,30,7,0)"))
        c.execute(text("INSERT INTO chain_claim_text VALUES (6,'Earth is round'),(7,'CO2 absorbs infrared')"))
        c.execute(text("INSERT INTO claim VALUES (60,'Earth is round'),(70,'CO2 absorbs infrared')"))
        c.execute(text("INSERT INTO claim_topic VALUES (60,1),(70,2)"))
    # the centroid step needs pgvector; make every article 'embedding-less' so it is skipped
    import types
    fake = types.ModuleType("articles.claim_indexer"); fake._article_summary_embedding = lambda db, aid: None
    monkeypatch.setitem(sys.modules, "articles.claim_indexer", fake)
    monkeypatch.setattr(tac, "_alert", lambda *a, **k: None)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def test_free_topic_bound_taken_topic_reported_pass_completes(db):
    res = tac.reconcile_topic_articles(db)
    assert "error" not in res, res                      # the pass no longer aborts
    rows = dict(db.execute(text("SELECT article_id, topic_id FROM topic_article ORDER BY article_id")).fetchall())
    assert rows == {1: 1, 2: None, 3: 2}                # 3 bound to the free topic; 2 left an orphan
    assert res["contested"] == 1


def test_contested_orphans_lists_the_pair(db):
    assert [tuple(r) for r in tac.contested_orphans(db)] == [(2, 1, 1)]


def test_old_binding_would_have_violated_the_constraint(db):
    """Documents the bug: binding article 2 to topic 1 is exactly what the constraint forbids."""
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE topic_article SET topic_id = 1 WHERE article_id = 2")); db.flush()
    db.rollback()
