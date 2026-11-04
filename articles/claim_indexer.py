# app/claim_indexer.py
"""
Cross-topic claim indexer.

When a new article is generated, find existing on-chain claims that
belong in it and insert them into the best-matching section.

Uses simple stem-word overlap for matching — no embedding dependency.
"""
import re
import logging
from typing import Set, Dict, Any, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

from semantic import find_best_onchain_match, find_all_onchain_matches, OVERLAY_THRESHOLD

# Topical relevance threshold for inserting on-chain claims into an
# article. Cosine similarity between the claim's embedding and the
# article's summary embedding (title + topic_key + first sentence of
# each section). Tuned to:
#   - Reject cross-domain content (e.g. a sex-change claim does NOT
#     belong in the Climate Change article even though both texts
#     contain the word "change").
#   - Accept on-domain dissent (e.g. a Milankovitch-cycles claim DOES
#     belong in the Climate Change article even if it argues against
#     the article's main thesis — admitting dissenting on-chain
#     content is the protocol's job).
RELEVANCE_THRESHOLD = 0.5

# Simple stop words
_STOP = {"a","an","the","is","are","was","were","be","been","being","have","has",
         "had","do","does","did","will","would","shall","should","may","might",
         "can","could","of","in","to","for","with","on","at","by","from","as",
         "into","through","during","about","and","but","or","not","no","its",
         "it","this","that","these","those","than","very","just","also"}


def _stems(text: str) -> Set[str]:
    """Extract lowercased word stems (crude: strip common suffixes)."""
    words = re.findall(r'[a-zA-Z]{2,}', text.lower())
    result = set()
    for w in words:
        if w in _STOP:
            continue
        # Crude stemming: strip common suffixes
        for suffix in ("ing", "tion", "sion", "ness", "ment", "ity", "ous",
                       "ive", "able", "ible", "ally", "ful", "less", "ly",
                       "ed", "er", "est", "es", "al", "en"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[:-len(suffix)]
                break
        result.add(w)
    return result


def _section_stems(section_sentences: List[str], heading: str) -> Set[str]:
    """Get all stems from a section's heading + sentences."""
    all_text = heading + " " + " ".join(section_sentences)
    return _stems(all_text)


def find_best_section(
    db: Session,
    article_id: int,
    claim_text: str,
    *,
    section_vec_by_id: Optional[dict] = None,
    claim_vec: Optional[list] = None,
) -> Optional[int]:
    """Find the section_id that best matches a claim, or None if no good match.

    Optional precomputed vectors (patch06):
        section_vec_by_id: { section_id: vec } — section embeddings already
            computed by the caller. Skips re-embedding sections.
        claim_vec: vec — the claim's embedding already computed by the
            caller. Skips re-embedding the claim.

    When passed in, find_best_section becomes a pure lookup with one
    cosine_similarity call per section (no network).
    """
    sections = db.execute(sql_text(
        "SELECT sec.section_id, sec.heading "
        "FROM article_section sec WHERE sec.article_id = :a ORDER BY sec.sort_order"
    ), {"a": article_id}).fetchall()

    if not sections:
        return None

    # Try embedding similarity first (accurate for temporal/semantic placement)
    try:
        from embedding import embed
        from similarity import cosine_similarity
        if claim_vec is None:
            claim_vec = embed(claim_text)
        best_id = None
        best_sim = -1.0
        for sec_id, heading in sections:
            sec_vec = (section_vec_by_id or {}).get(sec_id)
            if sec_vec is None:
                sents = db.execute(sql_text(
                    "SELECT text FROM article_sentence WHERE section_id = :s ORDER BY sort_order"
                ), {"s": sec_id}).fetchall()
                sec_text = heading + ". " + " ".join(r[0] for r in sents[:8])
                sec_vec = embed(sec_text)
            sim = cosine_similarity(claim_vec, sec_vec)
            if sim > best_sim:
                best_sim = sim
                best_id = sec_id
        if best_sim >= 0.25:
            return best_id
        logger.debug("No section above embedding threshold (best=%.3f)", best_sim)
    except Exception as e:
        logger.debug("Embedding section match failed, falling back to stems: %s", e)

    # Fallback: stem overlap
    claim_st = _stems(claim_text)
    if not claim_st:
        return None
    best_id = None
    best_overlap = 0
    for sec_id, heading in sections:
        sents = db.execute(sql_text(
            "SELECT text FROM article_sentence WHERE section_id = :s ORDER BY sort_order"
        ), {"s": sec_id}).fetchall()
        sec_st = _section_stems([r[0] for r in sents], heading)
        overlap = len(claim_st & sec_st)
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = sec_id
    return best_id if best_overlap >= 1 else None


def _article_summary_text(db: Session, article_id: int) -> Optional[str]:
    """Build a representative text summary of an article for embedding.

    Title + topic_key + first sentence of each section. This gives a
    concise but topically-faithful signal: the title and topic_key
    establish the domain, and the first sentence of each section
    captures the article's structure without the noise of long bodies.
    """
    art = db.execute(sql_text(
        "SELECT title, topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not art:
        return None
    title, topic_key = art
    sec_rows = db.execute(sql_text(
        "SELECT sec.section_id, sec.heading "
        "FROM article_section sec WHERE sec.article_id = :a "
        "ORDER BY sec.sort_order"
    ), {"a": article_id}).fetchall()
    parts = [str(title or ""), str(topic_key or "")]
    for sec_id, heading in sec_rows:
        if heading:
            parts.append(str(heading))
        first_sent = db.execute(sql_text(
            "SELECT text FROM article_sentence "
            "WHERE section_id = :s AND is_hidden = FALSE "
            "ORDER BY sort_order LIMIT 1"
        ), {"s": sec_id}).fetchone()
        if first_sent and first_sent[0]:
            parts.append(str(first_sent[0]))
    summary = ". ".join(p.strip() for p in parts if p and p.strip())
    return summary or None


def _article_summary_embedding(db: Session, article_id: int):
    """Embed the article summary, or None on failure."""
    summary = _article_summary_text(db, article_id)
    if not summary:
        return None
    try:
        from embedding import embed
        return embed(summary)
    except Exception as e:
        logger.debug("Article summary embedding failed for article %d: %s", article_id, e)
        return None


def _claim_relevance_score(claim_text: str, article_vec) -> Optional[float]:
    """Cosine similarity between claim and article summary, or None on failure."""
    if article_vec is None:
        return None
    try:
        from embedding import embed
        from similarity import cosine_similarity
        cv = embed(claim_text)
        return float(cosine_similarity(cv, article_vec))
    except Exception as e:
        logger.debug("Claim relevance embedding failed: %s", e)
        return None


def is_claim_relevant_to_article(
    db: Session,
    article_id: int,
    claim_text: str,
    article_vec=None,
) -> bool:
    """Check if a claim is topically relevant to an article.

    Strategy:
      1. Strict substring match: if the topic_key or title appears as a
         substring in the claim text, accept (cheap fast-path; high
         precision).
      2. Embedding similarity: cosine(claim, article_summary) ≥
         RELEVANCE_THRESHOLD.
      3. Fallback (embedding failure only): reject. We deliberately do
         NOT fall back to stem overlap, which produced the false
         positive that motivated this fix — "change" matched between
         "Climate Change" and "...can change the sex of a human being"
         and the unrelated claim was injected into the article.

    Args:
        db: SQLAlchemy session.
        article_id: ID in topic_article.
        claim_text: The candidate on-chain claim text.
        article_vec: Optional pre-computed article summary embedding.
            Pass this in when checking many claims against the same
            article to avoid re-embedding the article each time.
    """
    art = db.execute(sql_text(
        "SELECT title, topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not art:
        return False

    title, topic_key = art
    claim_lower = (claim_text or "").lower()
    title_lower = (title or "").lower().strip()
    topic_lower = (topic_key or "").lower().strip()

    # Strategy 1: strict substring match on title or topic_key.
    # Only match on non-trivial keys (≥ 4 chars) to avoid false positives
    # from short common words.
    if topic_lower and len(topic_lower) >= 4 and topic_lower in claim_lower:
        return True
    if title_lower and len(title_lower) >= 4 and title_lower in claim_lower:
        return True

    # Strategy 2: embedding similarity.
    if article_vec is None:
        article_vec = _article_summary_embedding(db, article_id)
    sim = _claim_relevance_score(claim_text, article_vec)
    if sim is not None:
        if sim >= RELEVANCE_THRESHOLD:
            logger.debug(
                "Relevance accept: article=%d sim=%.3f claim='%s'",
                article_id, sim, (claim_text or "")[:60],
            )
            return True
        logger.debug(
            "Relevance reject: article=%d sim=%.3f < %.2f claim='%s'",
            article_id, sim, RELEVANCE_THRESHOLD, (claim_text or "")[:60],
        )
        return False

    # Strategy 3: embedding unavailable — be strict, reject.
    # The historical stem-overlap fallback caused the very bug this
    # function exists to prevent. Better to miss a relevant claim than
    # to pollute an article with cross-domain content.
    logger.warning(
        "Relevance check fell through (no embedding available) for "
        "article=%d; rejecting claim conservatively: '%s'",
        article_id, (claim_text or "")[:60],
    )
    return False


def _ensure_sentence_embeddings(db: Session, article_id: int) -> int:
    """patch_crosslane_embed: ensure every VISIBLE sentence in the article has an
    embedding BEFORE grouping/weeding read them. Without this, group_topic cannot overlay
    a claim onto its matching prose sentence (so the claim is INSERTED as a duplicate) and
    the Stage-B weeding pass is inert (claim_vecs empty). Replaces the retired persist_dedup
    embed backfill. Claim sentences reuse the authoritative chain_claim_text embedding
    (matched by text — reset-independent, no API call); prose is embed_batch'd. Idempotent:
    fills only NULL embeddings. Returns the count that were missing."""
    missing = db.execute(sql_text(
        "SELECT s.sentence_id FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.is_hidden = FALSE AND s.embedding IS NULL"
    ), {"a": article_id}).fetchall()
    if not missing:
        return 0
    # (1) reuse chain_claim_text embeddings for sentences whose text is a known claim
    db.execute(sql_text(
        "UPDATE article_sentence AS s SET embedding = ct.embedding "
        "FROM chain_claim_text ct, article_section sec "
        "WHERE s.section_id = sec.section_id AND sec.article_id = :a "
        "AND s.embedding IS NULL AND ct.claim_text = s.text AND ct.embedding IS NOT NULL"
    ), {"a": article_id})
    db.commit()
    # (2) embed the remainder (prose) via the batch embedder
    rem = db.execute(sql_text(
        "SELECT s.sentence_id, s.text FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.is_hidden = FALSE AND s.embedding IS NULL "
        "AND s.text IS NOT NULL AND btrim(s.text) <> ''"
    ), {"a": article_id}).fetchall()
    reused = len(missing) - len(rem)
    if rem:
        try:
            from embedding import embed_batch
            vecs = embed_batch([t for _, t in rem])
            for (sid, _), vec in zip(rem, vecs):
                db.execute(sql_text(
                    "UPDATE article_sentence SET embedding = (:v)::vector WHERE sentence_id = :sid"
                ), {"v": "[" + ",".join(repr(float(x)) for x in vec) + "]", "sid": sid})
            db.commit()
        except Exception as e:
            logger.warning("_ensure_sentence_embeddings: prose embed failed for article %d: %s", article_id, e)
            db.rollback()
    logger.info("_ensure_sentence_embeddings: article %d — %d reused from chain_claim_text, %d embedded",
                article_id, reused, len(rem))
    return len(missing)


def index_existing_claims_into_article(db: Session, article_id: int):
    """unified-sole-authority injection: group_topic drives injection + hiding.

    Resets is_hidden (idempotent), groups claims+sentences via the unified
    module, injects each visible canonical claim (overlay onto a grouped
    sentence or insert), hides non-canonical sentence members, then weeds prose
    sentences that are >= WEED_THRESHOLD similar to a kept claim (claim-priority).
    """
    import os
    from articles.article_store import insert_sentence, update_sentence_post_id
    GROUP_THRESHOLD = float(os.getenv("VSP_GROUP_THRESHOLD", "0.95"))
    WEED_THRESHOLD = float(os.getenv("VSP_WEED_THRESHOLD", "0.78"))

    # resolve topic_key for this article
    row = db.execute(sql_text(
        "SELECT topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not row:
        logger.info("index: no topic_article for %d", article_id)
        return
    topic_key = row[0]

    # ── reset is_hidden for this article's sentences (idempotent baseline) ──
    db.execute(sql_text(
        "UPDATE article_sentence SET is_hidden = FALSE, post_id = NULL WHERE section_id IN "
        "(SELECT section_id FROM article_section WHERE article_id = :a)"
    ), {"a": article_id})
    db.commit()

    # ── patch_crosslane_embed: embed sentences before grouping/weeding read them ──
    _ensure_sentence_embeddings(db, article_id)

    # ── Stage A: unified grouping (claims + sentences) ──
    try:
        from unified_grouping_db import group_topic, make_similar
        from unified_grouping import KIND_CLAIM, KIND_SENTENCE
        import unified_grouping as _ug
        _ug.HIGH_THRESHOLD = GROUP_THRESHOLD  # honor config grouping bar
        groups = group_topic(db, topic_key)
    except Exception as e:
        logger.warning("unified grouping failed for article %d (%s); leaving as-is", article_id, e)
        return

    # index existing article sentences by sentence_id for overlay/hide
    sent_rows = db.execute(sql_text(
        "SELECT s.sentence_id, s.text, s.post_id, s.section_id "
        "FROM article_sentence s JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a"
    ), {"a": article_id}).fetchall()
    sent_by_id = {r[0]: {"text": r[1], "post_id": r[2], "section_id": r[3]} for r in sent_rows}

    hide_ids = set()
    kept_claim_sids = []   # (sentence_id) of sentences carrying a claim post_id
    attached_pids = set()  # patch_canonical_authority: canonicals already attached this run
    indexed_count = 0

    for g in groups:
        if not g.visible:
            # whole group hidden -> hide any of its sentence members present
            for m in g.members:
                if m.kind == KIND_SENTENCE and m.id in sent_by_id:
                    hide_ids.add(m.id)
            continue

        canon = g.canonical
        # sentence members of this group (present in the article)
        sent_members = [m for m in g.members if m.kind == KIND_SENTENCE and m.id in sent_by_id]

        if canon.kind == KIND_CLAIM:
            # patch_canonical_authority: claim_dupe_group is the SINGLE authority for
            # which member represents a dupe group. Resolve the article-side pick to
            # the authoritative canonical (post_id + text); ungrouped -> fall back.
            _row = db.execute(sql_text(
                "SELECT g2.canonical_post_id, ct2.claim_text "
                "FROM chain_claim_text ct "
                "JOIN claim_dupe_group g2 ON ct.dupe_group_id = g2.group_id "
                "JOIN chain_claim_text ct2 ON ct2.post_id = g2.canonical_post_id "
                "WHERE ct.post_id = :p"), {"p": canon.id}).fetchone()
            canon_pid = _row[0] if _row else canon.id
            canon_text = _row[1] if (_row and _row[1]) else canon.text
            if canon_pid in attached_pids:
                # authoritative canonical already carries a sentence (the article-side
                # pass split a dupe pair into two groups) -> hide, never double-attach
                for m in sent_members:
                    hide_ids.add(m.id)
                continue
            # patch_claim_verbatim_overlay: overlay a claim's post_id ONLY onto a
            # sentence whose text EQUALS the claim (whitespace/case-normalized).
            # INVARIANT: a claim carrier sentence must display the claim's OWN
            # text — never overlay onto a stance-similar but differently-worded
            # prose sentence (2026-07-19 post 12 Sicknick: the article showed a
            # paraphrase the user never staked). The old primary loop took the
            # first unclaimed GROUPED sentence with no text check; that is the
            # substitution bug. We now require exact text: first among grouped
            # sentence members, then any unclaimed article sentence. If none
            # matches the claim text, target_sid stays None and we INSERT the
            # claim's own text below (and hide the paraphrase as a grouped dupe).
            target_sid = None
            _canon_norm = " ".join(canon_text.split()).lower()
            for m in sent_members:
                _info = sent_by_id.get(m.id)
                if (_info and _info["post_id"] is None
                        and " ".join(_info["text"].split()).lower() == _canon_norm):
                    target_sid = m.id
                    break
            if target_sid is None:
                for _sid, _info in sent_by_id.items():
                    if (_info["post_id"] is None and _sid not in hide_ids
                            and " ".join(_info["text"].split()).lower() == _canon_norm):
                        target_sid = _sid
                        break
            if target_sid is not None:
                update_sentence_post_id(db, target_sid, canon_pid)
                sent_by_id[target_sid]["post_id"] = canon_pid  # keep in-run view fresh
                attached_pids.add(canon_pid)
                kept_claim_sids.append(target_sid)
                indexed_count += 1
                # hide the OTHER sentence members (grouped dupes)
                for m in sent_members:
                    if m.id != target_sid:
                        hide_ids.add(m.id)
            else:
                # no grouped sentence present -> insert the claim into best section
                try:
                    from articles.claim_indexer import find_best_section
                    sec_id = find_best_section(db, article_id, canon_text)
                    if sec_id is not None:
                        last = db.execute(sql_text(
                            "SELECT sentence_id FROM article_sentence WHERE section_id = :s "
                            "ORDER BY sort_order DESC LIMIT 1"
                        ), {"s": sec_id}).fetchone()
                        new_sid = insert_sentence(db, sec_id, last[0] if last else None, canon_text)
                        update_sentence_post_id(db, new_sid, canon_pid)
                        attached_pids.add(canon_pid)
                        kept_claim_sids.append(new_sid)
                        indexed_count += 1
                except Exception as e:
                    logger.warning("insert claim %s failed: %s", canon.id, e)
                # hide all grouped sentence members (they dup the now-injected claim)
                for m in sent_members:
                    hide_ids.add(m.id)
        else:
            # sentence-only group: keep canonical sentence, hide the rest
            ck = canon.id
            for m in sent_members:
                if m.id != ck:
                    hide_ids.add(m.id)

    # ── Stage B: claim-priority weeding (prose >= WEED_THRESHOLD to a kept claim) ──
    try:
        similar = make_similar()
        # embeddings for kept-claim sentences + candidate prose sentences
        def _emb(sid):
            r = db.execute(sql_text(
                "SELECT embedding::text FROM article_sentence WHERE sentence_id = :s"
            ), {"s": sid}).fetchone()
            if not r or not r[0]:
                return None
            v = r[0].strip()
            return [float(x) for x in v[1:-1].split(",")] if v.startswith("[") else None
        claim_vecs = [(sid, _emb(sid)) for sid in kept_claim_sids]
        claim_vecs = [(sid, v) for sid, v in claim_vecs if v is not None]
        for sid, info in sent_by_id.items():
            if info["post_id"] is not None or sid in hide_ids:
                continue  # skip claim-linked or already-hidden
            pv = _emb(sid)
            if pv is None:
                continue
            for csid, cv in claim_vecs:
                if csid == sid:
                    continue
                if float(similar(pv, cv)) >= WEED_THRESHOLD:
                    hide_ids.add(sid)
                    break
    except Exception as e:
        logger.warning("weeding pass failed for article %d: %s", article_id, e)

    # ── apply hides once ──
    # patch_inject_idem_guard: never hide a sentence that carries a claim
    # post_id. After the reset (edit 1) clears post_ids, a freshly re-linked
    # canonical carrier has post_id=None in sent_by_id and can match the
    # weeding pass; kept_claim_sids is the authoritative carrier set, so drop
    # those ids from hide_ids before applying. (idempotent: guarded by marker.)
    hide_ids -= set(kept_claim_sids)

    # patch_inject_never_hide_carrier: INVARIANT — a sentence carrying a claim
    # post_id is a staked claim and must never be hidden, no matter which run
    # attached it (the guard above only covers carriers re-linked THIS run).
    # A hidden carrier severs the on-chain stake from the displayed article,
    # which is the worst failure mode a truth-staking display can have.
    if hide_ids:
        _carriers = db.execute(sql_text(
            "SELECT sentence_id FROM article_sentence "
            "WHERE sentence_id = ANY(:ids) AND post_id IS NOT NULL"
        ), {"ids": list(hide_ids)}).fetchall()
        hide_ids -= {r[0] for r in _carriers}

    if hide_ids:
        try:
            db.execute(sql_text(
                "UPDATE article_sentence SET is_hidden = TRUE WHERE sentence_id = ANY(:ids)"
            ), {"ids": list(hide_ids)})
        except Exception as e:
            logger.warning("apply hides failed: %s", e)
    db.commit()

    logger.info("unified inject: article %d — injected %d claims, hid %d sentences",
                article_id, indexed_count, len(hide_ids))

    # patch_article_orphans: a sentence whose text IS an on-chain claim must never be shown as prose. The reset above
    # clears every post_id and Stage A re-attaches only this topic's claims, so a claim created from inside this
    # article but filed under another topic (post 13, typed in Climate Change, filed under Economic Systems) came back
    # as an unlinked sentence with a "make this a claim" prompt. Settle those: attach when the claim is this topic's or
    # relevant here, hide otherwise (it is shown in its own topic's article).
    try:
        settle_orphan_claim_sentences(db, article_id)
    except Exception as e:
        logger.warning("settle orphans failed for article %d: %s", article_id, e)


def _norm_text(t: str) -> str:
    return " ".join((t or "").split()).lower()


def settle_orphan_claim_sentences(db: Session, article_id: int, *, article_vec=None) -> dict:
    """patch_article_orphans. For one article: every visible sentence with no post_id whose normalized text equals an
    on-chain claim's text is either attached to that claim (same topic, or relevant to this article) or hidden (it
    belongs elsewhere). Returns {"attached": n, "hidden": n}. Never touches sentences that already carry a post_id."""
    art = db.execute(sql_text("SELECT topic_key FROM topic_article WHERE article_id = :a"), {"a": article_id}).fetchone()
    if not art:
        return {"attached": 0, "hidden": 0}
    topic_key = (art[0] or "").lower().strip()
    orphans = db.execute(sql_text(
        "SELECT s.sentence_id, s.text FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.post_id IS NULL AND s.is_hidden = FALSE AND s.text IS NOT NULL"
    ), {"a": article_id}).fetchall()
    if not orphans:
        return {"attached": 0, "hidden": 0}
    # on-chain claims by normalized text, with the topic the claim table filed them under
    claims = {}
    for pid, ctext, topic in db.execute(sql_text(
        "SELECT ct.post_id, ct.claim_text, c.topic FROM chain_claim_text ct "
        "LEFT JOIN claim c ON c.claim_text = ct.claim_text AND c.post_id IS NOT NULL"
    )).fetchall():
        key = _norm_text(ctext)
        if key and key not in claims:
            claims[key] = (int(pid), ctext, (topic or "").lower().strip())
    # a claim already carried by another sentence of this article must not be attached twice
    carried = {int(r[0]) for r in db.execute(sql_text(
        "SELECT s.post_id FROM article_sentence s JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.post_id IS NOT NULL"), {"a": article_id}).fetchall()}
    attached = hidden = 0
    for sid, stext in orphans:
        hit = claims.get(_norm_text(stext))
        if not hit:
            continue
        pid, ctext, ctopic = hit
        if pid in carried:
            db.execute(sql_text("UPDATE article_sentence SET is_hidden = TRUE WHERE sentence_id = :s"), {"s": sid})
            hidden += 1
            continue
        keep = (ctopic == topic_key) or is_claim_relevant_to_article(db, article_id, ctext, article_vec)
        if keep:
            db.execute(sql_text("UPDATE article_sentence SET post_id = :p WHERE sentence_id = :s"), {"p": pid, "s": sid})
            carried.add(pid)
            attached += 1
            logger.info("orphan claim sentence %d attached to post %d in article %d", sid, pid, article_id)
        else:
            db.execute(sql_text("UPDATE article_sentence SET is_hidden = TRUE WHERE sentence_id = :s"), {"s": sid})
            hidden += 1
            logger.info("orphan claim sentence %d (post %d, topic '%s') hidden in article %d ('%s')", sid, pid, ctopic, article_id, topic_key)
    db.commit()
    return {"attached": attached, "hidden": hidden}


def repair_orphan_claim_sentences(db: Session) -> dict:
    """patch_article_orphans: the one-off for data already on disk — settle every article, invalidate the cache of
    those that changed (cached_response cleared) so the next read serves the settled article and rebuilds the cache. Returns per-article counts for the articles touched."""
    out = {}
    for article_id, topic_key in db.execute(sql_text("SELECT article_id, topic_key FROM topic_article")).fetchall():
        r = settle_orphan_claim_sentences(db, int(article_id))
        if r["attached"] or r["hidden"]:
            out[topic_key] = r
            db.execute(sql_text("UPDATE topic_article SET cached_response = NULL, last_refreshed_at = NULL WHERE article_id = :a"), {"a": article_id})
            db.commit()
    return out


def cross_index_claim_into_all_articles(db: Session, claim_text: str, post_id: int):
    """Index a single on-chain claim into ALL relevant articles.

    Called by chain/indexer.py whenever a new claim is discovered.
    Checks every existing article for topical relevance and inserts
    the claim into the best-matching section if not already present.
    """
    from articles.article_store import insert_sentence, update_sentence_post_id

    # Get all articles
    articles = db.execute(sql_text(
        "SELECT article_id, topic_key, title FROM topic_article ORDER BY updated_at DESC"
    )).fetchall()

    if not articles:
        return

    claim_lower = claim_text.lower().strip()
    indexed_into = 0

    for article_id, topic_key, title in articles:
        # Check if claim is already in this article (by post_id — authoritative dedup)
        existing = db.execute(sql_text(
            "SELECT 1 FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "WHERE sec.article_id = :a AND s.post_id = :pid "
            "LIMIT 1"
        ), {"a": article_id, "pid": post_id}).fetchone()

        if existing:
            continue

        # Check topical relevance
        if not is_claim_relevant_to_article(db, article_id, claim_text):
            continue

        # Find best section (uses its own embedding caching path)
        sec_id = find_best_section(db, article_id, claim_text)
        if sec_id is None:
            continue

        # Insert at end of section
        try:
            last_sent = db.execute(sql_text(
                "SELECT sentence_id FROM article_sentence "
                "WHERE section_id = :s ORDER BY sort_order DESC LIMIT 1"
            ), {"s": sec_id}).fetchone()

            after_id = last_sent[0] if last_sent else None
            new_sid = insert_sentence(db, sec_id, after_id, claim_text)
            update_sentence_post_id(db, new_sid, post_id)
            indexed_into += 1
            logger.info(
                "Cross-indexed claim post_id=%d into article '%s' section %d",
                post_id, topic_key, sec_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to cross-index claim post_id=%d into article %d: %s",
                post_id, article_id, e,
            )

    if indexed_into > 0:
        logger.info(
            "Cross-indexed claim post_id=%d ('%s') into %d article(s)",
            post_id, claim_text[:40], indexed_into,
        )



def cleanup_irrelevant_claims_from_article(
    db_or_factory,
    article_id: int,
) -> int:
    """Remove previously-injected on-chain claim sentences that fail
    the current relevance gate.

    Safe to run repeatedly. Lock profile:
      Phase 1 — read all candidate rows + the article-summary embedding
                source (only AccessShareLock).
      Phase 2 — call embed() per candidate to score relevance. No DB
                writes during this phase, so the (slow) network calls
                cannot block another process holding row locks on
                article_sentence or article_section.
      Phase 3 — issue a single batched DELETE for the doomed rows,
                then commit. One fast write transaction.

    Conservative criterion: only removes sentences whose text byte-
    equals the on-chain claim_text (i.e. were INSERTED by the indexer
    rather than merely overlaid onto an existing LLM-generated
    sentence). Overlaid sentences carry a post_id but their text is
    the LLM's wording, which is paraphrased; deleting those would
    take legitimate article content with them.

    Args:
        db_or_factory: an open SQLAlchemy Session OR a session factory
            callable. If a factory is passed, cleanup runs in its own
            fresh session and is fully isolated from any caller
            transaction. Passing a factory is recommended.
        article_id: ID in topic_article.

    Returns the number of sentences removed.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.orm import Session as _Session

    # Use a fresh session if the caller passed a factory. Avoids
    # poisoning the caller's transaction state on any error here, and
    # avoids deadlocking against the caller's own pending writes.
    if isinstance(db_or_factory, _Session):
        db = db_or_factory
        owns_session = False
    else:
        db = db_or_factory()
        owns_session = True

    try:
        # ── Phase 1: read everything we might need (read-only) ──
        article_vec = _article_summary_embedding(db, article_id)
        if article_vec is None:
            logger.debug(
                "Cleanup: article %d summary embedding unavailable; skipping",
                article_id,
            )
            return 0

        rows = db.execute(sql_text(
            "SELECT s.sentence_id, s.post_id, c.claim_text "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN claim c ON c.post_id = s.post_id "
            "WHERE sec.article_id = :a "
            "  AND s.post_id IS NOT NULL "
            "  AND LOWER(TRIM(s.text)) = LOWER(TRIM(c.claim_text))"
        ), {"a": article_id}).fetchall()
        if not rows:
            return 0

        # ── Phase 2: decide what to delete (no writes, embed() calls) ──
        doomed = []  # list of (sentence_id, post_id, claim_text)
        for sentence_id, post_id, claim_text in rows:
            if is_claim_relevant_to_article(
                db, article_id, claim_text, article_vec=article_vec,
            ):
                continue
            doomed.append((int(sentence_id), int(post_id), str(claim_text)))

        if not doomed:
            return 0

        # ── Phase 3: single batched DELETE + commit ──
        try:
            db.execute(sql_text(
                "DELETE FROM article_sentence "
                "WHERE sentence_id = ANY(:ids)"
            ), {"ids": [d[0] for d in doomed]})
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(
                "Cleanup: batched DELETE failed for article %d: %s",
                article_id, e,
            )
            return 0

        for sid, pid, ctext in doomed:
            logger.info(
                "Cleanup: removed irrelevant injected sentence %d "
                "(post_id=%d) from article %d: '%s'",
                sid, pid, article_id, (ctext or "")[:60],
            )
        return len(doomed)
    finally:
        if owns_session:
            try:
                db.close()
            except Exception:
                pass
