"""app/articles/topic_article_canonical.py — one-article-per-topic enforcement.

Two halves:

  RESOLUTION (write path): resolve_article_for_topic_id() — get-or-create an
  article keyed on the centroid-resolved topic_id, NOT on a fuzzy topic string.
  This is what ensure_article_for_claim should have used; string prefix-matching
  is what let "January 6" / "6th" / "6th insurrection" become three articles for
  one semantic topic.

  SELF-HEALING (worker): reconcile_topic_articles() — find any topic_id bound to
  >1 article, collapse to a single canonical article (regenerate clean prose,
  re-inject ALL staked claims from every duplicate, retire the extras). Runs on
  the worker loop. Every deletion is logged and alerted. Keyed strictly on
  topic_id (disambiguated identity) — NEVER on name — so two same-named /
  different-meaning topics (distinct centroids -> distinct topic_id) are never
  fused. A verification asserts the topic COUNT is unchanged by any collapse:
  we merge articles, never topics.

Claim-preservation invariant (same as the article-inject fixes): no staked
claim (a sentence carrying a post_id) is ever dropped; every one is re-homed to
the canonical article before any duplicate article is deleted.
"""
import logging
import os

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger("topic_article_canonical")

RECONCILE_ENABLED = os.getenv("VSP_TOPIC_ARTICLE_RECONCILE_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on"
)


def _alert(kind, message, **fields):
    try:
        import notify
        notify.send_alert(kind, message, **fields)
    except Exception as e:
        logger.warning("topic_article alert delivery failed: %s", e)


def resolve_topic_id_for_claim(db: Session, claim_text: str):
    """Return the centroid-resolved topic_id for a claim, or None. Uses the SAME
    semantic path as topic_reconciler: claim embedding <=> topic centroid. No
    string matching — this is what makes same-name/different-meaning safe."""
    row = db.execute(sql_text(
        "SELECT t.topic_id, (1 - (t.centroid <=> x.embedding))::real AS sim "
        "FROM chain_claim_text x "
        "JOIN topic t ON t.centroid IS NOT NULL "
        "WHERE x.claim_text = :ct AND x.embedding IS NOT NULL "
        "ORDER BY t.centroid <=> x.embedding ASC LIMIT 1"
    ), {"ct": claim_text}).fetchone()
    return row[0] if row else None


def resolve_article_for_topic_id(db: Session, topic_id: int, title: str) -> int | None:
    """Get-or-create the SINGLE article for a topic_id. If one exists (by the
    topic_id binding), return it. Else create one bound to topic_id. This is the
    write-path invariant: article identity = topic_id, not string. Relies on the
    UNIQUE(topic_id) constraint to make the create race-safe (ON CONFLICT)."""
    if topic_id is None:
        return None
    row = db.execute(sql_text(
        "SELECT article_id FROM topic_article WHERE topic_id = :tid LIMIT 1"
    ), {"tid": topic_id}).fetchone()
    if row:
        return row[0]
    # create bound to topic_id; topic_key kept for back-compat display/search.
    # ON CONFLICT (topic_id) DO NOTHING covers a concurrent create; re-select after.
    key = (title or "").strip().lower()
    db.execute(sql_text(
        "INSERT INTO topic_article (topic_key, title, topic_id) "
        "VALUES (:k, :t, :tid) ON CONFLICT (topic_id) DO NOTHING"
    ), {"k": key, "t": title, "tid": topic_id})
    db.commit()
    row = db.execute(sql_text(
        "SELECT article_id FROM topic_article WHERE topic_id = :tid LIMIT 1"
    ), {"tid": topic_id}).fetchone()
    return row[0] if row else None


def _claims_on_article(db: Session, article_id: int):
    """All staked claims (post_id + text) carried by an article's sentences."""
    return db.execute(sql_text(
        "SELECT DISTINCT s.post_id, s.text FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.post_id IS NOT NULL"
    ), {"a": article_id}).fetchall()


def _delete_article(db: Session, article_id: int):
    """Delete an article and its sections/sentences. Caller must have re-homed
    any staked claims FIRST — this asserts none remain as a safety net."""
    remaining = db.execute(sql_text(
        "SELECT COUNT(*) FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.post_id IS NOT NULL"
    ), {"a": article_id}).scalar()
    if remaining and int(remaining) > 0:
        raise RuntimeError(
            f"refusing to delete article {article_id}: {remaining} staked claims still attached")
    secs = db.execute(sql_text(
        "SELECT section_id FROM article_section WHERE article_id = :a"), {"a": article_id}).fetchall()
    for (sid,) in secs:
        db.execute(sql_text("DELETE FROM article_sentence WHERE section_id = :s"), {"s": sid})
    db.execute(sql_text("DELETE FROM article_section WHERE article_id = :a"), {"a": article_id})
    db.execute(sql_text("DELETE FROM topic_article WHERE article_id = :a"), {"a": article_id})


def collapse_topic(db: Session, topic_id: int) -> dict:
    """Collapse all articles bound to (or matching) one topic_id into a single
    canonical article. Regenerates clean prose for the canonical, re-injects
    every staked claim from every duplicate, retires the extras. Keyed on
    topic_id only. Returns a summary. Never drops a claim."""
    from articles.article_gen import generate_article
    from articles.article_store import store_article, get_article
    from articles.claim_indexer import index_existing_claims_into_article

    # topic label/title
    trow = db.execute(sql_text("SELECT label FROM topic WHERE topic_id = :t"), {"t": topic_id}).fetchone()
    if not trow:
        return {"topic_id": topic_id, "error": "no such topic"}
    label = trow[0]

    # every article bound to this topic_id (binding set by _bind_unbound_articles:
    # claim-linkage + centroid, never title string). Keyed on topic_id only, so we
    # never merge across different topics.
    arts = db.execute(sql_text(
        "SELECT article_id, topic_key, title, topic_id FROM topic_article "
        "WHERE topic_id = :t"
    ), {"t": topic_id}).fetchall()
    if len(arts) <= 1:
        # already canonical; just ensure the binding is set
        if arts and arts[0][3] is None:
            db.execute(sql_text("UPDATE topic_article SET topic_id = :t WHERE article_id = :a"),
                       {"t": topic_id, "a": arts[0][0]})
            db.commit()
        return {"topic_id": topic_id, "articles": len(arts), "collapsed": 0}

    # gather all staked claims across all duplicate articles (dedup by post_id)
    claims = {}
    for (aid, _k, _t, _tid) in arts:
        for post_id, ctext in _claims_on_article(db, aid):
            claims.setdefault(post_id, ctext)

    # regenerate a clean canonical article for the topic label
    result = generate_article(label)
    # store_article on the label upserts by topic_key; capture the article_id
    store_article(db, label, result["title"], result["sections"])
    db.commit()
    canon = get_article(db, label)
    if not canon:
        return {"topic_id": topic_id, "error": "canonical generation failed"}
    canon_id = canon["article_id"]
    # bind the canonical article to the topic_id
    db.execute(sql_text("UPDATE topic_article SET topic_id = :t WHERE article_id = :a"),
               {"t": topic_id, "a": canon_id})
    db.commit()

    # re-inject every staked claim onto the canonical article. First run the
    # standard injector (honors never-hide-carrier + text-overlay invariants),
    # then EXPLICITLY ensure each gathered claim is present — the general injector
    # attaches by topic relevance and may not cover every specific historical
    # claim, so we backstop it deterministically. Idempotent: skips any claim
    # already carried.
    index_existing_claims_into_article(db, canon_id)
    db.commit()
    from articles.article_store import insert_sentence
    from articles.claim_indexer import find_best_section
    canon_have = {r[0] for r in _claims_on_article(db, canon_id)}
    for post_id, ctext in claims.items():
        if post_id in canon_have:
            continue
        try:
            sec_id = find_best_section(db, canon_id, ctext)
            if sec_id is None:
                sec_id = db.execute(sql_text(
                    "SELECT section_id FROM article_section WHERE article_id = :a "
                    "ORDER BY sort_order LIMIT 1"), {"a": canon_id}).scalar()
            if sec_id is not None:
                last = db.execute(sql_text(
                    "SELECT sentence_id FROM article_sentence WHERE section_id = :s "
                    "ORDER BY sort_order DESC LIMIT 1"), {"s": sec_id}).fetchone()
                new_sid = insert_sentence(db, sec_id, last[0] if last else None, ctext)
                db.execute(sql_text(
                    "UPDATE article_sentence SET post_id = :p WHERE sentence_id = :s"),
                    {"p": post_id, "s": new_sid})
                db.commit()
        except Exception as e:
            logger.warning("explicit re-inject of claim %s failed: %s", post_id, e)

    # verify every claim is now carried by the canonical article before deleting
    canon_posts = {r[0] for r in _claims_on_article(db, canon_id)}
    missing = set(claims) - canon_posts
    if missing:
        # do NOT delete anything — a claim would be lost. Alert loudly.
        _alert("topic_article_collapse_incomplete",
               f"topic {topic_id} ({label}): {len(missing)} staked claims not on canonical "
               f"article after re-inject ({sorted(missing)}); leaving duplicates in place",
               topic_id=topic_id, missing=sorted(missing))
        return {"topic_id": topic_id, "error": "claims_missing", "missing": sorted(missing)}

    # safe to retire the extras (everything that isn't the canonical). Every
    # staked claim is now CONFIRMED present on the canonical article (checked
    # above), so the duplicate copies on the retiring articles are redundant —
    # detach them (null post_id) so the delete-guard's "no staked claim attached"
    # invariant holds. The claim itself is preserved on canonical; only its
    # redundant duplicate rendering on a retired article is removed.
    retired = 0
    for (aid, _k, _t, _tid) in arts:
        if aid == canon_id:
            continue
        db.execute(sql_text(
            "UPDATE article_sentence SET post_id = NULL WHERE section_id IN "
            "(SELECT section_id FROM article_section WHERE article_id = :a) "
            "AND post_id IS NOT NULL"
        ), {"a": aid})
        db.commit()
        _delete_article(db, aid)
        retired += 1
    db.commit()
    _alert("topic_article_collapsed",
           f"topic {topic_id} ({label}): collapsed {len(arts)} articles -> 1 canonical "
           f"(article {canon_id}); retired {retired}; {len(claims)} staked claims preserved",
           topic_id=topic_id, canonical=canon_id, retired=retired, claims=len(claims))
    return {"topic_id": topic_id, "articles": len(arts), "collapsed": retired,
            "canonical": canon_id, "claims_preserved": len(claims)}


_CONTESTED: list = []  # patch_topicart_bind_unique: (orphan article_id, topic_id, canonical article_id) per run


def contested_orphans(db: Session):
    """patch_topicart_bind_unique: orphans (topic_id NULL) whose claim-linked topic already has an article.
    Reported, not bound: the unique constraint (migration 311) forbids a second article per topic, and merging
    an orphan's claims into the canonical article is collapse_topic's job in a follow-up."""
    return db.execute(sql_text(
        "SELECT sub.article_id, sub.topic_id, x.article_id AS canonical FROM ("
        "  SELECT s.article_id, ct.topic_id, "
        "         ROW_NUMBER() OVER (PARTITION BY s.article_id ORDER BY COUNT(*) DESC) AS rnk "
        "  FROM (SELECT sec.article_id, asent.post_id "
        "        FROM article_sentence asent "
        "        JOIN article_section sec ON asent.section_id = sec.section_id "
        "        WHERE asent.post_id IS NOT NULL) s "
        "  JOIN chain_claim_text cct ON cct.post_id = s.post_id "
        "  JOIN claim c ON c.claim_text = cct.claim_text "
        "  JOIN claim_topic ct ON ct.claim_id = c.claim_id "
        "  GROUP BY s.article_id, ct.topic_id) sub "
        "JOIN topic_article ta ON ta.article_id = sub.article_id AND ta.topic_id IS NULL "
        "JOIN topic_article x ON x.topic_id = sub.topic_id "
        "WHERE sub.rnk = 1"
    )).fetchall()


def _bind_unbound_articles(db: Session) -> int:
    """Bind any topic_article with NULL topic_id to a topic. Priority:
    (1) the topic its staked claims link to (claim_topic, centroid-based);
    (2) for claim-less articles, the topic whose centroid is nearest the
    article's summary embedding. Never binds by title string. Returns count bound.
    This is the semantic binding that title-matching missed ("6" vs "6th")."""
    _CONTESTED.clear()
    # (1) claim-linkage binding
    # patch_topicart_bind_unique: migration 311 made topic_article.topic_id UNIQUE, so binding an orphan to a topic
    # that already has an article violated the constraint and aborted the whole hourly reconcile (prod, every hour
    # since at least 2026-10-11). Bind only where the topic is free (NOT EXISTS) and only ONE orphan per topic in this
    # statement (per_topic = 1 — two orphans pointing at the same free topic would collide inside the UPDATE itself,
    # which is what dev showed on 2026-10-12 — patch_topicart_bind_unique_2). Contested orphans are reported, never bound. The step is also isolated
    # in its own transaction so a surprise here cannot abort the rest of the pass.
    try:
        db.execute(sql_text(
            "UPDATE topic_article AS ta SET topic_id = pick.topic_id "
            "FROM ("
            "  SELECT q.article_id, q.topic_id FROM ("
            "    SELECT sub.article_id, sub.topic_id, "
            "           ROW_NUMBER() OVER (PARTITION BY sub.topic_id ORDER BY sub.article_id) AS per_topic "
            "    FROM ("
            "      SELECT s.article_id, ct.topic_id, "
            "             ROW_NUMBER() OVER (PARTITION BY s.article_id ORDER BY COUNT(*) DESC) AS rnk "
            "      FROM (SELECT sec.article_id, asent.post_id "
            "            FROM article_sentence asent "
            "            JOIN article_section sec ON asent.section_id = sec.section_id "
            "            WHERE asent.post_id IS NOT NULL) s "
            "      JOIN chain_claim_text cct ON cct.post_id = s.post_id "
            "      JOIN claim c ON c.claim_text = cct.claim_text "
            "      JOIN claim_topic ct ON ct.claim_id = c.claim_id "
            "      GROUP BY s.article_id, ct.topic_id) sub "
            "    JOIN topic_article orphan ON orphan.article_id = sub.article_id AND orphan.topic_id IS NULL "
            "    WHERE sub.rnk = 1 "
            "      AND NOT EXISTS (SELECT 1 FROM topic_article x WHERE x.topic_id = sub.topic_id)"
            "  ) q WHERE q.per_topic = 1"
            ") pick "
            "WHERE pick.article_id = ta.article_id AND ta.topic_id IS NULL"
        ))
        db.commit()
    except Exception as e:  # pragma: no cover - defensive: never let binding abort the pass
        db.rollback()
        logger.warning("claim-linkage binding skipped this run: %s", e)
    # (2) centroid-summary binding for still-unbound (claim-less) articles
    from articles.claim_indexer import _article_summary_embedding
    unbound = db.execute(sql_text(
        "SELECT article_id FROM topic_article WHERE topic_id IS NULL"
    )).fetchall()
    bound = 0
    for (aid,) in unbound:
        try:
            vec = _article_summary_embedding(db, aid)
            if vec is None:
                continue
            row = db.execute(sql_text(
                "SELECT topic_id FROM topic WHERE centroid IS NOT NULL "
                "ORDER BY centroid <=> (:v)::vector ASC LIMIT 1"
            ), {"v": str(list(vec))}).fetchone()
            if row:
                # patch_topicart_bind_unique: one article per topic (migration 311) — a taken topic is contested
                taken = db.execute(sql_text(
                    "SELECT article_id FROM topic_article WHERE topic_id = :t"), {"t": row[0]}).fetchone()
                if taken:
                    _CONTESTED.append((aid, row[0], taken[0]))
                    continue
                db.execute(sql_text(
                    "UPDATE topic_article SET topic_id = :t WHERE article_id = :a"
                ), {"t": row[0], "a": aid})
                bound += 1
        except Exception as e:
            logger.warning("centroid bind failed for article %d: %s", aid, e)
    db.commit()
    return bound


def find_multi_article_topics(db: Session):
    """Every topic with >1 article, counted by the topic_id BINDING (claim-linkage
    + centroid, set by _bind_unbound_articles). NOT by title string — that is the
    fragility that caused the fan-out. Callers must _bind_unbound_articles first."""
    return db.execute(sql_text(
        "SELECT t.topic_id, t.label, COUNT(a.article_id) AS n "
        "FROM topic t JOIN topic_article a ON a.topic_id = t.topic_id "
        "GROUP BY t.topic_id, t.label HAVING COUNT(a.article_id) > 1 "
        "ORDER BY n DESC"
    )).fetchall()


def reconcile_topic_articles(db: Session) -> dict:
    """Self-healing pass: collapse every multi-article topic. Never raises.
    Asserts the topic COUNT is unchanged (we merge articles, never topics)."""
    summary = {"topics_scanned": 0, "collapsed": 0, "errors": 0}
    try:
        topics_before = db.execute(sql_text("SELECT COUNT(*) FROM topic")).scalar()
        # bind any NULL-topic_id articles first (claim-linkage + centroid), so
        # detection sees the true article->topic membership, not title strings.
        bound = _bind_unbound_articles(db)
        if bound:
            logger.info("reconcile: bound %d previously-unbound articles", bound)
        # patch_topicart_bind_unique: orphans whose topic is taken are reported once per run, never bound
        contested = list(contested_orphans(db)) + list(_CONTESTED)
        summary["contested"] = len(contested)
        if contested:
            logger.warning("reconcile: %d orphan article(s) contest a topic that already has an article: %s",
                           len(contested), ", ".join(f"article {a} -> topic {t} (canonical {c})" for a, t, c in contested))
        multi = find_multi_article_topics(db)
        summary["topics_scanned"] = len(multi)
        if not RECONCILE_ENABLED:
            if multi:
                _alert("topic_article_reconcile_disabled",
                       f"{len(multi)} multi-article topics detected; reconciler disabled "
                       f"(VSP_TOPIC_ARTICLE_RECONCILE_ENABLED=false)")
            return summary
        for (topic_id, label, n) in multi:
            try:
                r = collapse_topic(db, topic_id)
                if r.get("collapsed"):
                    summary["collapsed"] += 1
                if r.get("error"):
                    summary["errors"] += 1
            except Exception as e:
                db.rollback()
                summary["errors"] += 1
                logger.exception("collapse_topic %s failed", topic_id)
                _alert("topic_article_collapse_error", f"topic {topic_id} ({label}): {e}",
                       topic_id=topic_id)
        topics_after = db.execute(sql_text("SELECT COUNT(*) FROM topic")).scalar()
        # patch_topicart_bind_unique_2: one INFO line per completed pass, so a healthy reconcile is visible in the log
        logger.info("reconcile: pass complete — bound %d, contested %d, multi-article topics %d, collapsed %d, errors %d",
                    bound, summary.get("contested", 0), summary["topics_scanned"], summary["collapsed"], summary["errors"])
        # INVARIANT: collapsing articles must NEVER change the number of topics.
        # If it did, we fused meanings — a far worse bug than the fan-out.
        if topics_after != topics_before:
            _alert("topic_article_topic_count_changed",
                   f"CRITICAL: topic count changed {topics_before} -> {topics_after} during "
                   f"article reconcile — meanings may have been fused; investigate",
                   before=topics_before, after=topics_after)
            summary["errors"] += 1
        return summary
    except Exception as e:
        db.rollback()
        logger.warning("reconcile_topic_articles failed: %s", e)
        return {"error": str(e)}
