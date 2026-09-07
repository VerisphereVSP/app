from moderation import check_content_fast
# app/article_routes.py
"""
Article API endpoints.

GET  /api/article/{topic}                    → full article with VS-enriched sentences
POST /api/article/{topic}/generate           → AI-generate + store (idempotent)
POST /api/article/sentence/insert            → add sentence to a section
POST /api/article/sentence/{id}/edit         → replace sentence (create new + challenge old)
POST /api/article/sentence/{id}/register     → register sentence on-chain (lazy)
POST /api/article/sentence/cleanup           → AI grammar cleanup
GET  /api/disambiguate                       → typeahead search
"""
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from sqlalchemy import text as sql_text
from rate_limit import ai_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["article"])


# ── Request models ──────────────────────────────────────

class GenerateRequest(BaseModel):
    refresh: bool = False

class InsertRequest(BaseModel):
    section_id: int
    after_sentence_id: Optional[int] = None
    text: str

class EditRequest(BaseModel):
    new_text: str

class CleanupRequest(BaseModel):
    text: str
    topic: str = ""

class RegisterRequest(BaseModel):
    """Empty — just triggers on-chain registration."""
    pass


# ── Helpers ─────────────────────────────────────────────

def _enrich_sentences(article: dict, db=None) -> dict:
    """Add VS, stake totals from indexed DB. Falls back to zeros if no DB."""
    if db is None:
        try:
            from db import get_session_factory
            db = get_session_factory()()
            _owns = True
        except Exception:
            return article
    else:
        _owns = False

    try:
        from chain.chain_db import get_stake_totals, get_verity_score
        for section in article.get("sections", []):
            for sent in section.get("sentences", []):
                pid = sent.get("post_id")
                if pid is not None:
                    try:
                        s, ch = get_stake_totals(db, pid)
                        sent["stake_support"] = s
                        sent["stake_challenge"] = ch
                        sent["verity_score"] = get_verity_score(db, pid)
                    except Exception:
                        sent["stake_support"] = 0
                        sent["stake_challenge"] = 0
                        sent["verity_score"] = 0
                else:
                    sent["stake_support"] = 0
                    sent["stake_challenge"] = 0
                    sent["verity_score"] = 0
    finally:
        if _owns:
            db.close()

    # Display-time moderation filter
    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            mod = check_content_fast(sent.get("text", ""))
            if not mod.allowed:
                sent["text"] = "[Content hidden — policy violation]"
                sent["moderated"] = True

    # Semantic dedup: remove off-chain near-duplicates
    try:
        _semantic_dedup(article)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Semantic dedup failed: %s", e)

    return article




def _semantic_dedup(article: dict):
    """Remove duplicate and near-duplicate sentences from article display.
    Rules:
      - On-chain sentences (post_id != null): ALWAYS kept, even if near-dupes of each other
      - Off-chain near-duplicate of an on-chain sentence: REMOVED
      - Off-chain near-duplicate of an earlier off-chain sentence: REMOVED (keep first)

    Performance: every sentence in the article is embedded in ONE
    batched embed_batch() call upfront, then the dedup logic is pure
    in-memory cosine similarity. Replaces O(N) sequential network
    calls per API render with a single batched call.
    """
    from embedding import embed_batch
    from similarity import cosine_similarity

    DEDUP_THRESHOLD = 0.70  # Cosine similarity above this = near-duplicate

    # ── Pass 1: collect every sentence (on-chain + off-chain) and
    #            assign it a stable index for the batched embed call ──
    sentence_records = []  # list of (section_index, sent_index_in_section, sent_dict, is_onchain, text)
    texts_to_embed = []
    for sec_idx, section in enumerate(article.get("sections", [])):
        for s_idx, sent in enumerate(section.get("sentences", [])):
            text = (sent.get("text") or "").strip()
            is_onchain = sent.get("post_id") is not None
            if not text and not is_onchain:
                # Empty off-chain sentence: drop entirely (parity with old code).
                sentence_records.append((sec_idx, s_idx, sent, is_onchain, "", -1))
                continue
            if not text:
                # Empty on-chain (rare): skip embedding but keep.
                sentence_records.append((sec_idx, s_idx, sent, is_onchain, "", -1))
                continue
            sentence_records.append((sec_idx, s_idx, sent, is_onchain, text, len(texts_to_embed)))
            texts_to_embed.append(text)

    # ── Pass 2: one batched embed call for every sentence in the article ──
    vecs = []
    if texts_to_embed:
        try:
            vecs = embed_batch(texts_to_embed)
        except Exception:
            vecs = []  # signals fallback below

    # If batched embedding failed entirely, drop the dedup step (fail
    # open). The original code's per-sentence except-pass meant that
    # on embed errors, the sentence was kept as-is; preserve that.
    if not vecs or len(vecs) != len(texts_to_embed):
        # Just clean up empty-text off-chain sentences and return.
        for section in article.get("sections", []):
            section["sentences"] = [
                s for s in section.get("sentences", [])
                if (s.get("text") or "").strip() or s.get("post_id") is not None
            ]
        return

    # ── Pass 3: collect on-chain embeddings ──
    onchain_embeddings = [
        vecs[idx]
        for (_, _, _, is_onchain, _, idx) in sentence_records
        if is_onchain and idx >= 0
    ]

    # ── Pass 4: filter each section using cached vecs ──
    kept_offchain_embeddings = []
    # Build a lookup: (sec_idx, s_idx) -> filter decision
    new_sections_sentences = {sec_idx: [] for sec_idx in range(len(article.get("sections", [])))}

    # Walk sentence_records in document order so off-chain dedup
    # preserves "keep first" semantics across sections.
    for sec_idx, s_idx, sent, is_onchain, text, vec_idx in sentence_records:
        if is_onchain:
            # Keep on-chain unconditionally (skip empty-text on-chain
            # too — that matches the old behavior of falling through).
            if text or sent.get("post_id") is not None:
                new_sections_sentences[sec_idx].append(sent)
            continue

        # Off-chain: drop if empty text.
        if not text or vec_idx < 0:
            continue

        vec = vecs[vec_idx]

        # Check against on-chain sentences
        is_dupe = False
        for oc_vec in onchain_embeddings:
            if cosine_similarity(vec, oc_vec) >= DEDUP_THRESHOLD:
                is_dupe = True
                break
        if is_dupe:
            continue  # near-dupe of on-chain sentence

        # Check against already-kept off-chain sentences
        for kept_vec in kept_offchain_embeddings:
            if cosine_similarity(vec, kept_vec) >= DEDUP_THRESHOLD:
                is_dupe = True
                break
        if is_dupe:
            continue  # near-dupe of earlier off-chain sentence

        new_sections_sentences[sec_idx].append(sent)
        kept_offchain_embeddings.append(vec)

    # ── Apply filtered sentences back into the article ──
    for sec_idx, section in enumerate(article.get("sections", [])):
        section["sentences"] = new_sections_sentences.get(sec_idx, [])

def _ensure_sentence_in_claim_db(db: Session, text: str) -> int:
    """Ensure a sentence exists in the claim table, return claim_id."""
    from semantic import ensure_claim
    return ensure_claim(db, text)


def _link_unlinked_sentences(db: Session, article: dict):
    """For any article sentence with post_id=NULL, find matching on-chain claims
    using semantic embedding similarity — not just exact text match."""
    from articles.article_store import update_sentence_post_id, invalidate_article_cache
    from semantic import find_best_onchain_match
    patched = 0
    already_used = set()

    # Collect existing post_ids
    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            pid = sent.get("post_id")
            if pid is not None:
                already_used.add(pid)

    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            if sent.get("post_id") is not None:
                continue
            sid = sent.get("sentence_id")
            text = sent.get("text", "").strip()
            if not sid or not text:
                continue
            try:
                match = find_best_onchain_match(db, text, exclude_post_ids=already_used)
                if match:
                    update_sentence_post_id(db, sid, match["post_id"])
                    _invalidate_and_rebuild(db, sid)
                    sent["post_id"] = match["post_id"]
                    already_used.add(match["post_id"])
                    patched += 1
                    logger.info(
                        "Semantic link: sentence %d -> post %d (sim=%.3f) '%s' <-> '%s'",
                        sid, match["post_id"], match["similarity"],
                        text[:30], match["claim_text"][:30],
                    )
            except Exception as e:
                logger.warning("Failed to semantic-link sentence %d: %s", sid, e)
    if patched:
        logger.info("Patched %d unlinked sentences with on-chain post_ids", patched)




def _onchain_normalize(text: str) -> bytes:
    """Byte-exact port of PostRegistry._normalizeBytes: collapse ASCII
    whitespace runs to one 0x20, lowercase ASCII A-Z only, trim a trailing
    space. Unicode passes through untouched, exactly as on-chain."""
    raw = text.encode("utf-8")
    buf = bytearray()
    last_space = True
    for b in raw:
        if b in (0x20, 0x09, 0x0A, 0x0D):
            if not last_space:
                buf.append(0x20); last_space = True
            continue
        if 0x41 <= b <= 0x5A:
            b += 32
        buf.append(b); last_space = False
    if buf and buf[-1] == 0x20:
        buf.pop()
    return bytes(buf)


def _require_sentence_matches_chain(db: Session, sentence_id: int, post_id: int) -> None:
    """M4: refuse to link unless the sentence text equals the on-chain claim text
    under the registry's own normalization. 404 unknown sentence, 409 not yet
    indexed/readable, 422 mismatch."""
    row = db.execute(sql_text("SELECT text FROM article_sentence WHERE sentence_id = :sid"),
                     {"sid": sentence_id}).fetchone()
    if not row:
        raise HTTPException(404, "Unknown sentence")
    sentence_text = row[0] or ""
    chain_text = None
    try:
        r = db.execute(sql_text("SELECT claim_text FROM chain_claim_text WHERE post_id = :p"),
                       {"p": post_id}).fetchone()
        if r and r[0] is not None:
            chain_text = r[0]
    except Exception:
        chain_text = None
    if chain_text is None:
        # indexer lag right after creation: read the registry directly
        try:
            from chain.registry_reader import get_claim_text
            chain_text = get_claim_text(post_id)
        except Exception:
            chain_text = None
    if chain_text is None:
        raise HTTPException(409, "Claim not yet indexed; retry shortly")
    if _onchain_normalize(sentence_text) != _onchain_normalize(chain_text):
        raise HTTPException(422, "Sentence text does not match the on-chain claim text")


def _increment_view_count(db: Session, article_id: int):
    """Increment the view counter for an article. Non-fatal."""
    try:
        db.execute(sql_text(
            "UPDATE topic_article SET view_count = COALESCE(view_count, 0) + 1 "
            "WHERE article_id = :a"
        ), {"a": article_id})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _invalidate_and_rebuild(db: Session, sentence_id: int):
    """Invalidate article cache and trigger immediate background rebuild."""
    import threading
    from db import get_session_factory
    _invalidate_and_rebuild(db, sentence_id)
    # Find the topic key for this sentence
    try:
        row = db.execute(sql_text(
            "SELECT ta.topic_key FROM topic_article ta "
            "JOIN article_section sec ON sec.article_id = ta.article_id "
            "JOIN article_sentence s ON s.section_id = sec.section_id "
            "WHERE s.sentence_id = :sid LIMIT 1"
        ), {"sid": sentence_id}).fetchone()
        if row:
            topic_key = row[0]
            def _bg_rebuild():
                try:
                    Sess = get_session_factory()
                    from articles.article_store import build_and_cache_response
                    build_and_cache_response(Sess, topic_key)
                    logger.info("Immediate cache rebuild complete: %s", topic_key)
                except Exception as e:
                    logger.warning("Immediate cache rebuild failed for %s: %s", topic_key, e)
            threading.Thread(target=_bg_rebuild, daemon=True).start()
            logger.info("Immediate cache rebuild queued for %s", topic_key)
    except Exception as e:
        logger.warning("Could not queue rebuild for sentence %d: %s", sentence_id, e)

# ── Endpoints ───────────────────────────────────────────

@router.get("/article/{topic:path}/version")
def get_article_version(topic: str, db: Session = Depends(get_db)):
    """Return the current article version hash.
    Frontend polls this every 30s to detect updates."""
    row = db.execute(sql_text(
        "SELECT response_hash FROM topic_article WHERE LOWER(topic_key) = LOWER(:t)"
    ), {"t": topic}).fetchone()
    if not row:
        return {"hash": None}
    return {"hash": row[0]}



@router.get("/article/{topic:path}")
def get_article(topic: str, db: Session = Depends(get_db)):
    """Get a full article. Serves pre-built cached JSON — zero processing."""
    from articles.article_store import ensure_tables
    ensure_tables(db)

    # Try cached response first (one SELECT, zero processing)
    row = db.execute(sql_text(
        "SELECT article_id, cached_response FROM topic_article "
        "WHERE LOWER(topic_key) = LOWER(:t)"
    ), {"t": topic}).fetchone()

    if row and row[1]:
        # Cache hit — serve as-is
        _increment_view_count(db, row[0])
        return row[1]

    # No cache: check if article exists but cache is cold
    from articles.article_store import get_article as load_article
    article = load_article(db, topic)
    if article:
        # Build cache in background; serve enriched version this one time
        import threading
        def _bg_cache(topic_key):
            try:
                from db import get_session_factory
                from articles.article_store import build_and_cache_response
                build_and_cache_response(get_session_factory(), topic_key)
            except Exception as e:
                logger.debug("Background cache build failed: %s", e)
        threading.Thread(target=_bg_cache, args=(topic,), daemon=True).start()

        _increment_view_count(db, article["article_id"])
        return _enrich_sentences(article, db)

    # No article at all — generate (only slow path: first visit ever)
    return _generate_and_store(topic, db, refresh=False)


@router.post("/article/{topic}/generate")
@ai_rate_limit
def generate_article_endpoint(topic: str, req: GenerateRequest, request: Request,
                              db: Session = Depends(get_db)):
    # M5 (security review 2026-09): without `request` in the signature the
    # decorator could not find a client IP and every caller shared one global
    # "ai:unknown" bucket — one client could lock out AI features for all.
    """Generate (or regenerate) an article for a topic."""
    return _generate_and_store(topic, db, refresh=req.refresh)


def _generate_and_store(topic: str, db: Session, refresh: bool) -> dict:
    print(f"GENERATE_AND_STORE CALLED: topic={topic} refresh={refresh}")
    from articles.article_store import ensure_tables, get_article as load_article, store_article
    from articles.article_gen import generate_article
    ensure_tables(db)

    if not refresh:
        existing = load_article(db, topic)
        if existing:
            return _enrich_sentences(existing)

    logger.info("Generating article for '%s'", topic)
    result = generate_article(topic)
    # Validate generated title matches the requested topic
    gen_title = (result.get("title") or "").lower().strip()
    topic_lower = topic.lower().strip()
    if gen_title and topic_lower not in gen_title and gen_title not in topic_lower:
        logger.warning("Generation rejected: got '%s' for topic '%s', retrying", result.get("title"), topic)
        result = generate_article(topic)  # One retry

    store_article(db, topic, result["title"], result["sections"])
    article = load_article(db, topic)
    if not article:
        raise HTTPException(500, "Failed to load article after storing")

    # Also ensure each sentence is in the claim table for embedding search
    for section in article["sections"]:
        for sent in section["sentences"]:
            try:
                _ensure_sentence_in_claim_db(db, sent["text"])
            except Exception as e:
                logger.warning("Failed to ensure claim for '%s': %s", sent["text"][:50], e)

    # Index existing on-chain claims into this new article
    try:
        from articles.claim_indexer import index_existing_claims_into_article
        index_existing_claims_into_article(db, article["article_id"])
        # Re-load to include any indexed claims
        article = load_article(db, topic)
    except Exception as e:
        logger.warning("Claim indexing failed (non-fatal): %s", e)

    # Run dedup + cache build in background for future instant serving
    import threading
    article_id_for_bg = article["article_id"]
    def _bg_cache(topic_key, art_id):
        try:
            from db import get_session_factory
            from articles.article_store import persist_dedup, build_and_cache_response
            Sess = get_session_factory()
            db = Sess()
            try:
                persist_dedup(db, art_id)
            finally:
                db.close()
            build_and_cache_response(Sess, topic_key)
        except Exception as e:
            logger.debug("Post-generate dedup+cache build failed: %s", e)
    threading.Thread(target=_bg_cache, args=(topic, article_id_for_bg), daemon=True).start()

    return _enrich_sentences(article, db)


@router.post("/article/sentence/insert")
def insert_sentence_endpoint(req: InsertRequest, db: Session = Depends(get_db)):
    """Insert a new sentence into a section."""
    from articles.article_store import ensure_tables, insert_sentence
    from articles.article_gen import split_into_sentences
    ensure_tables(db)

    sentences = split_into_sentences(req.text)
    inserted = []

    after_id = req.after_sentence_id
    for sent_text in sentences:
        sid = insert_sentence(db, req.section_id, after_id, sent_text)

        # Ensure in claim DB
        try:
            _ensure_sentence_in_claim_db(db, sent_text)
        except Exception:
            pass

        # Check if already on chain
        from sqlalchemy import text as sql_text
        existing_post = db.execute(sql_text(
            "SELECT post_id FROM claim WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) "
            "AND post_id IS NOT NULL LIMIT 1"
        ), {"t": sent_text}).fetchone()
        existing_pid = existing_post[0] if existing_post else None
        if existing_pid:
            from articles.article_store import update_sentence_post_id
            update_sentence_post_id(db, sid, existing_pid)
            _invalidate_and_rebuild(db, sid)

        inserted.append({"sentence_id": sid, "text": sent_text, "post_id": existing_pid})
        after_id = sid  # Chain insertions

    return {"inserted": inserted}


@router.post("/article/sentence/{sentence_id}/edit")
def edit_sentence_endpoint(sentence_id: int, req: EditRequest,
                           db: Session = Depends(get_db)):
    """Replace a sentence: creates new sentence(s) + marks old as replaced.

    The frontend is responsible for creating the on-chain challenge link
    (new_post_id challenges old_post_id) since that requires the user's wallet.
    """
    from articles.article_store import (
        ensure_tables, insert_sentence, mark_replaced,
    )
    from articles.article_gen import split_into_sentences
    from sqlalchemy import text as sql_text
    ensure_tables(db)

    # Get the old sentence's section_id
    old = db.execute(sql_text(
        "SELECT section_id, sort_order, text, post_id FROM article_sentence "
        "WHERE sentence_id = :id"
    ), {"id": sentence_id}).fetchone()
    if not old:
        raise HTTPException(404, "Sentence not found")

    section_id, old_order, old_text, old_post_id = old

    # PD-04: Block editing on-chain claims
    if old_post_id is not None:
        raise HTTPException(400,
            "Cannot edit an on-chain claim. "
            "Create a new claim and link it as evidence instead.")

    new_sentences = [req.new_text.strip()]
    created = []

    # PD-04: Determine if this is a friendly edit (similar meaning) or hostile.
    # Embed old_text and new_text ONCE, share the vectors and similarity
    # score across both the friendly-check and the section re-eval below.
    is_friendly = True
    _edit_old_vec = None
    _edit_new_vec = None
    _edit_sim = None
    try:
        from embedding import embed_batch
        from similarity import cosine_similarity
        new_text_stripped = req.new_text.strip()
        try:
            _vecs = embed_batch([old_text, new_text_stripped])
            _edit_old_vec, _edit_new_vec = _vecs[0], _vecs[1]
        except Exception:
            from embedding import embed
            _edit_old_vec = embed(old_text)
            _edit_new_vec = embed(new_text_stripped)
        _edit_sim = cosine_similarity(_edit_old_vec, _edit_new_vec)
        is_friendly = _edit_sim >= 0.80
        if not is_friendly:
            logger.info("Hostile edit detected (sim=%.3f): '%s' → '%s'",
                        _edit_sim, old_text[:40], new_text_stripped[:40])
    except Exception:
        pass  # Default to friendly if embedding fails

    # Re-evaluate section placement if text changed significantly.
    # Reuses _edit_sim computed above — no second embedding pass.
    target_section_id = section_id
    try:
        if _edit_sim is not None and _edit_sim < 0.85:
            from articles.claim_indexer import find_best_section
            art_row = db.execute(sql_text(
                "SELECT article_id FROM article_section WHERE section_id = :s"
            ), {"s": section_id}).fetchone()
            if art_row:
                better_section = find_best_section(
                    db, art_row[0], req.new_text.strip(),
                    claim_vec=_edit_new_vec,
                )
                if better_section and better_section != section_id:
                    target_section_id = better_section
                    logger.info("Edit moved to different section: %d -> %d (sim=%.3f)",
                                section_id, better_section, _edit_sim)
    except Exception as e:
        logger.debug("Section re-evaluation failed (keeping original): %s", e)

    after_id = sentence_id if target_section_id == section_id else None
    for sent_text in new_sentences:
        new_sid = insert_sentence(db, target_section_id, after_id, sent_text)
        try:
            _ensure_sentence_in_claim_db(db, sent_text)
        except Exception:
            pass

        # Check if this claim already exists on-chain
        existing_post = db.execute(sql_text(
            "SELECT post_id FROM claim WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) "
            "AND post_id IS NOT NULL LIMIT 1"
        ), {"t": sent_text}).fetchone()
        existing_pid = existing_post[0] if existing_post else None
        if existing_pid:
            from articles.article_store import update_sentence_post_id
            update_sentence_post_id(db, new_sid, existing_pid)
            _invalidate_and_rebuild(db, new_sid)

        created.append({
            "sentence_id": new_sid,
            "text": sent_text,
            "post_id": existing_pid,  # Frontend can skip createClaim if already on chain
        })
        after_id = new_sid

    # PD-04: Friendly edit = hide old sentence (user is improving the article)
    # Hostile edit = keep old visible (user tried to change meaning)
    if created:
        mark_replaced(db, sentence_id, created[0]["sentence_id"])
        if is_friendly:
            db.execute(sql_text(
                "UPDATE article_sentence SET is_hidden = TRUE WHERE sentence_id = :sid"
            ), {"sid": sentence_id})
            db.commit()
            logger.info("Friendly edit: hid old sentence %d", sentence_id)

    return {
        "old_sentence_id": sentence_id,
        "old_post_id": old_post_id,
        "created": created,
        "is_friendly": is_friendly,
    }


@router.post("/article/sentence/{sentence_id}/register")
def register_sentence_endpoint(sentence_id: int,
                               db: Session = Depends(get_db)):
    """Register a sentence on-chain. Called when a user first interacts with it.

    Note: actual on-chain tx is done client-side via wallet. This endpoint
    just links the sentence to its post_id after the client reports it.
    """
    # This is actually handled by a separate call — the client creates the
    # claim on-chain and then calls this to update the DB.
    # See: POST /api/article/sentence/{id}/link_post
    raise HTTPException(501,
        "Use /api/article/sentence/{id}/link_post with {post_id} instead")


class LinkPostRequest(BaseModel):
    post_id: int

@router.post("/article/sentence/{sentence_id}/link_post")
def link_post_endpoint(sentence_id: int, req: LinkPostRequest,
                       db: Session = Depends(get_db)):
    """Link a sentence to its on-chain post_id after client-side registration.

    M4 (security review 2026-09): this endpoint is unauthenticated by design
    (any reader may curate), so the INVARIANT it must uphold is that a score is
    never rendered next to text that is not the on-chain claim. The link is
    accepted only if the sentence text, normalized exactly as PostRegistry
    normalizes it, matches the indexed on-chain claim text (falling back to a
    live getClaim() read while the indexer catches up).
    """
    from articles.article_store import ensure_tables, update_sentence_post_id
    ensure_tables(db)
    _require_sentence_matches_chain(db, sentence_id, req.post_id)
    update_sentence_post_id(db, sentence_id, req.post_id)
    _invalidate_and_rebuild(db, sentence_id)

    # Rebuild article cache so the linked post_id appears immediately
    try:
        topic_row = db.execute(sql_text(
            "SELECT ta.topic_key FROM topic_article ta "
            "JOIN article_sentence s ON s.article_id = ta.article_id "
            "WHERE s.sentence_id = :sid"
        ), {"sid": sentence_id}).fetchone()
        if topic_row:
            import threading
            def _rebuild(tk):
                try:
                    from db import get_session_factory
                    from articles.article_store import build_and_cache_response
                    build_and_cache_response(get_session_factory(), tk)
                except Exception:
                    pass
            threading.Thread(target=_rebuild, args=(topic_row[0],), daemon=True).start()
    except Exception:
        pass

    return {"sentence_id": sentence_id, "post_id": req.post_id}


@router.post("/article/sentence/cleanup")
@ai_rate_limit
def cleanup_sentence_endpoint(req: CleanupRequest, request: Request):
    # M5: see generate_article_endpoint.
    """AI grammar/spelling cleanup. Returns original + suggested."""
    from articles.article_gen import cleanup_sentence
    original = req.text.strip()
    suggested = cleanup_sentence(original, topic=req.topic)
    return {"original": original, "suggested": suggested}


@router.get("/disambiguate")
def disambiguate_endpoint(q: str, db: Session = Depends(get_db)):
    """Typeahead disambiguation for search bar."""
    if not q or len(q.strip()) < 1:
        return {"results": []}
    from articles.article_store import ensure_tables, disambiguate
    ensure_tables(db)
    return {"results": disambiguate(db, q.strip())}


@router.get("/claims/{post_id}/stakes")
def get_user_stakes(post_id: int, user: str = None, db: Session = Depends(get_db)):
    """Get stake totals and user-specific stakes for a post_id."""
    try:
        from chain.chain_db import get_stake_totals, get_verity_score, get_user_stake
        support, challenge = get_stake_totals(db, post_id)
        result = {
            "post_id": post_id,
            "stake_support": support,
            "stake_challenge": challenge,
            "verity_score": get_verity_score(db, post_id),
            "user_support": 0,
            "user_challenge": 0,
        }
        if user:
            result["user_support"] = get_user_stake(db, user, post_id, 0)
            result["user_challenge"] = get_user_stake(db, user, post_id, 1)
        return result
    except Exception as e:
        logger.warning("Failed to read stakes for post %d: %s", post_id, e)
        return {"post_id": post_id, "stake_support": 0, "stake_challenge": 0,
                "verity_score": 0, "user_support": 0, "user_challenge": 0}

@router.get("/topics/popular")
def popular_topics(limit: int = 8, db: Session = Depends(get_db)):
    """Return the most-viewed topics for the landing page."""
    from sqlalchemy import text as sql_text
    rows = db.execute(sql_text(
        "SELECT topic_key, title, view_count FROM topic_article "
        "ORDER BY COALESCE(view_count, 0) DESC, title ASC LIMIT :l"
    ), {"l": limit}).fetchall()

    return {"topics": [
        {"key": r[0], "title": r[1], "views": r[2]}
        for r in rows
    ]}



class DetectTopicRequest(BaseModel):
    claim_text: str
    post_id: int

@router.post("/claims/detect-topic")
def detect_topic_endpoint(req: DetectTopicRequest, db: Session = Depends(get_db)):
    """Auto-detect topic for a standalone claim, store the association,
    and trigger background article generation if needed.
    Returns immediately with the detected topic."""
    from articles.topic_detect import detect_topic, ensure_article_for_claim, snap_topic

    # patch_detect_topic_fix: cluster-then-label here too — inherit a near-identical
    # existing claim's topic (>= snap threshold) so near-dup claims share article
    # placement; LLM detect only when there is no strong neighbour. snap_topic returns
    # None gracefully if this claim's embedding isn't computed yet.
    topic = snap_topic(db, req.post_id) or detect_topic(req.claim_text)
    if not topic:
        return {"topic": None, "status": "detection_failed"}

    # Store topic association in the claim table
    try:
        # keyed by claim_text (claim.post_id is NULL in practice -> the old
        # UPDATE silently matched 0 rows); only-if-empty mirrors the worker's guard
        # so this endpoint never overwrites an established topic.
        db.execute(sql_text(
            "UPDATE claim SET topic = :t "
            "WHERE claim_text = :ct AND (topic IS NULL OR btrim(topic) = '')"
        ), {"t": topic, "ct": req.claim_text})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    # Ensure article exists (generates in background if not)
    try:
        ensure_article_for_claim(db, req.claim_text, req.post_id, topic)
    except Exception as e:
        logger.warning("ensure_article_for_claim failed: %s", e)

    return {"topic": topic, "status": "ok"}



@router.post("/moderate")
def moderate_endpoint(req: CleanupRequest):
    """Check if content passes moderation. Returns {allowed, reason}."""
    from moderation import check_content
    result = check_content(req.text)
    return {"allowed": result.allowed, "reason": result.reason}



@router.post("/article/{topic:path}/refresh")
def refresh_article_endpoint(topic: str, db: Session = Depends(get_db)):
    """On-demand article refresh. Generates new content and merges with existing.
    Preserves all existing sentences and their on-chain claim links."""
    from articles.article_store import refresh_article, build_and_cache_response
    try:
        added = refresh_article(db, topic)
        # Rebuild cached response with new content
        import threading
        def _bg_cache(t):
            try:
                from db import get_session_factory
                build_and_cache_response(get_session_factory(), t)
            except Exception:
                pass
        threading.Thread(target=_bg_cache, args=(topic,), daemon=True).start()
        return {"refreshed": added, "topic": topic}
    except Exception as e:
        logger.warning("Article refresh failed for '%s': %s", topic, e)
        raise HTTPException(500, f"Refresh failed: {e}")

