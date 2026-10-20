# app/claim_locate.py
"""
Underline matcher for the VeriSphere browser extension.

The extension sends the salient phrases of the page it is on (title, wikilink
anchors, headings) plus the sentences currently in view; it gets back the
on-chain claims those sentences state, which is what it underlines.

Three stages, cheapest first:

  1. Candidates — claims whose text matches any of the page's phrases, via the
     GIN full-text index on chain_claim_text (idx_chain_claim_text_fts).
  2. Lexical containment — a claim "appears" in a sentence when (nearly) all of
     its significant tokens do. Free, exact, and the dominant real case since
     users write claims from article fragments. Crucially it catches an atomic
     claim embedded in a long compound sentence, which whole-sentence
     embeddings score below any usable vector threshold.
  3. Vector locate — one batched embedding call for the surviving sentences,
     then a pgvector search scoped to the candidate claims. Catches paraphrases,
     where there is semantic but not lexical overlap.

A sentence can state several claims ("Climate change threatens people with
increased flooding, extreme heat, …" states a flooding claim and a heat claim),
so each stage keeps every claim that clears its threshold for a sentence — up to
MAX_PER_SENTENCE, best first — and the sentence id appears in each of those
claims' groups. The extension ranks them, underlines the top one and offers the
rest. A generic claim whose words are all contained in a more specific matching
claim is dropped, so "climate change threatens people" never rides along with
the claims that actually say what the threat is.

No LLM is involved. Ported from the retired verity-api gateway's /article/match,
which ran the same algorithm over an ingestor-maintained copy of this corpus.

Nothing needs to seed the corpus when a claim is created: the chain indexer sees
PostCreated within a poll cycle and the derived-state worker embeds it, so a new
claim becomes matchable for everyone within seconds either way.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_db
from rate_limit import public_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claims", tags=["claims"])


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# A claim counts as lexically present when this fraction of its significant
# tokens appear in the sentence. Claims shorter than the token floor are too
# generic to match this way and are left to the vector stage.
LEX_CONTAINMENT = _env_float("VSP_LOCATE_CONTAINMENT", 0.85)
LEX_MIN_CLAIM_TOKENS = _env_int("VSP_LOCATE_MIN_CLAIM_TOKENS", 4)
# Cosine similarity floor for the vector stage.
VECTOR_THRESHOLD = _env_float("VSP_LOCATE_VECTOR_THRESHOLD", 0.8)
# How many claims one sentence may resolve to (best first).
MAX_PER_SENTENCE = _env_int("VSP_LOCATE_MAX_PER_SENTENCE", 4)
# Bounds on the work one request can ask for.
CANDIDATE_LIMIT = _env_int("VSP_LOCATE_CANDIDATE_LIMIT", 300)
MAX_SENTENCES = _env_int("VSP_LOCATE_MAX_SENTENCES", 400)
MAX_PHRASES = _env_int("VSP_LOCATE_MAX_PHRASES", 60)
MAX_SENTENCE_CHARS = _env_int("VSP_LOCATE_MAX_SENTENCE_CHARS", 2000)

_STOPWORDS: Set[str] = set(
    "the a an and or of to in on for with by from as at is are was were be been "
    "being this that these those it its their his her they he she we you i not "
    "no than then so such also into over under about after before between "
    "during within without".split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    """Significant lowercase word tokens: 3+ chars, stopwords dropped."""
    return [w for w in _WORD_RE.findall((text or "").lower())
            if len(w) >= 3 and w not in _STOPWORDS]


# ── Stage 1: candidates ────────────────────────────────────────────

def _candidate_claims(db: Session, phrases: List[str]) -> List[Tuple[int, str]]:
    """Claims whose text full-text matches any page phrase, best-ranked first."""
    cleaned: List[str] = []
    seen: Set[str] = set()
    for p in phrases:
        if not isinstance(p, str):
            continue
        p = p.strip()
        key = p.lower()
        if len(p) < 3 or key in seen:
            continue
        seen.add(key)
        cleaned.append(p)
        if len(cleaned) >= MAX_PHRASES:
            break
    if not cleaned:
        return []

    # OR every phrase into one tsquery: (plainto(:p0) || plainto(:p1) || ...).
    tsq = "(" + " || ".join(
        f"plainto_tsquery('english', :p{i})" for i in range(len(cleaned))
    ) + ")"
    params: Dict[str, Any] = {f"p{i}": p for i, p in enumerate(cleaned)}
    params["lim"] = CANDIDATE_LIMIT

    try:
        rows = db.execute(sql_text(f"""
            SELECT post_id, claim_text,
                   ts_rank(to_tsvector('english', claim_text), {tsq}) AS rank
              FROM chain_claim_text
             WHERE claim_text IS NOT NULL
               AND to_tsvector('english', claim_text) @@ {tsq}
             ORDER BY rank DESC
             LIMIT :lim
        """), params).fetchall()
    except Exception as e:
        logger.warning("locate: candidate lookup failed: %s", e)
        return []
    return [(int(r[0]), r[1] or "") for r in rows]


# ── Stage 2: lexical containment ───────────────────────────────────

Matches = Dict[int, List[Tuple[int, float]]]  # sentence index -> [(post_id, score)] best first


def _drop_subsumed(hits: List[Tuple[int, float, Set[str]]]) -> List[Tuple[int, float, Set[str]]]:
    """Drop a matching claim whose tokens are a strict subset of another matching
    claim's: the generic claim ("climate change threatens people") is implied by
    the specific one ("… with extreme heat") and would otherwise ride along with
    every specific claim in the sentence."""
    keep = []
    for post_id, score, toks in hits:
        if any(o_toks > toks for o_id, _, o_toks in hits if o_id != post_id):
            continue
        keep.append((post_id, score, toks))
    return keep


def _lexical_matches(
    sentence_tokens: List[Set[str]],
    candidates: List[Tuple[int, List[str]]],
    limit: int = MAX_PER_SENTENCE,
) -> Matches:
    """Candidates per sentence index by claim-token containment, best first."""
    out: Matches = {}
    for i, sent in enumerate(sentence_tokens):
        hits: List[Tuple[int, float, Set[str]]] = []
        for post_id, toks in candidates:
            if len(toks) < LEX_MIN_CLAIM_TOKENS:
                continue
            n = sum(1 for t in toks if t in sent)
            score = n / len(toks)
            if score >= LEX_CONTAINMENT:
                hits.append((post_id, score, set(toks)))
        if not hits:
            continue
        hits = _drop_subsumed(hits)
        # Highest containment first, ties broken toward the longer (more
        # specific) claim; the candidate order (full-text rank) settles the rest.
        hits.sort(key=lambda h: (-h[1], -len(h[2])))
        out[i] = [(post_id, score) for post_id, score, _ in hits[:limit]]
    return out


# ── Stage 3: vector locate ─────────────────────────────────────────

def _vector_matches(
    db: Session,
    texts: List[str],
    candidate_ids: List[int],
    limit: int = MAX_PER_SENTENCE,
) -> Matches:
    """Nearest candidate claims per sentence within the similarity threshold, best first."""
    from config import EMBEDDINGS_PROVIDER
    if EMBEDDINGS_PROVIDER == "stub" or not texts or not candidate_ids:
        return {}

    try:
        from embedding import embed_batch
        vecs = embed_batch(texts)
    except Exception as e:
        logger.warning("locate: embedding failed, lexical only: %s", e)
        return {}

    idxs: List[int] = []
    lits: List[str] = []
    for i, vec in enumerate(vecs):
        if not vec:
            continue
        idxs.append(i)
        lits.append("[" + ",".join(str(float(x)) for x in vec) + "]")
    if not idxs:
        return {}

    # One round trip for every sentence: LATERAL nearest-neighbour per vector,
    # scoped to the candidate claims (an exact scan over <= CANDIDATE_LIMIT
    # rows, so no ANN index is involved either way).
    try:
        rows = db.execute(sql_text("""
            WITH q AS (
                SELECT * FROM unnest(CAST(:idxs AS int[]), CAST(:vecs AS text[]))
                         AS t(idx, vec)
            )
            SELECT q.idx, m.post_id, m.sim
              FROM q
              CROSS JOIN LATERAL (
                  SELECT post_id, 1 - (embedding <=> q.vec::vector) AS sim
                    FROM chain_claim_text
                   WHERE embedding IS NOT NULL
                     AND post_id = ANY(:ids)
                     AND (embedding <=> q.vec::vector) <= :maxdist
                   ORDER BY embedding <=> q.vec::vector
                   LIMIT :k
              ) m
             ORDER BY q.idx, m.sim DESC
        """), {
            "idxs": idxs,
            "vecs": lits,
            "ids": candidate_ids,
            "maxdist": 1.0 - VECTOR_THRESHOLD,
            "k": max(1, limit),
        }).fetchall()
    except Exception as e:
        logger.warning("locate: vector search failed, lexical only: %s", e)
        return {}

    out: Matches = {}
    for r in rows:
        out.setdefault(int(r[0]), []).append((int(r[1]), float(r[2])))
    return out


def _merge_matches(lexical: Matches, vector: Matches, limit: int = MAX_PER_SENTENCE) -> Matches:
    """Per sentence, the union of both stages' claims, each at its more confident
    score (a near-1.0 containment beats a borderline vector score and vice
    versa), best first, capped."""
    out: Matches = {}
    for i in set(lexical) | set(vector):
        best: Dict[int, float] = {}
        for post_id, score in lexical.get(i, []) + vector.get(i, []):
            if score > best.get(post_id, -1.0):
                best[post_id] = score
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:limit]
        if ranked:
            out[i] = ranked
    return out


# ── Claim summaries ────────────────────────────────────────────────

def _summary(db: Session, post_id: int) -> Optional[Dict[str, Any]]:
    """The same claim summary the extension reads elsewhere, or None if the
    indexer hasn't caught up with this post yet."""
    from claim_views import claim_summary
    try:
        return claim_summary(post_id, db)
    except HTTPException:
        return None
    except Exception as e:
        logger.warning("locate: summary failed for post %d: %s", post_id, e)
        return None


# ── Endpoints ──────────────────────────────────────────────────────

@router.post("/locate")
@public_endpoint("/api/claims/locate", cost_tier="ai_batch", budget="locate")
def locate_claims(
    body: dict,
    request: Request = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Locate on-chain claims in the sentences of a page.

    Body: {title, url, revisionId, phrases: [str], sentences: [{sentenceId, text}]}
    Returns groups shaped like the claim summaries the extension already reads,
    one per matched claim, with the sentence ids that stated it.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    raw_phrases = body.get("phrases") or []
    raw_sentences = body.get("sentences") or []
    if not isinstance(raw_phrases, list) or not isinstance(raw_sentences, list):
        raise HTTPException(400, "phrases and sentences must be arrays")

    title = str(body.get("title") or "").strip()[:200]

    sentences: List[Tuple[str, str]] = []
    for s in raw_sentences[:MAX_SENTENCES]:
        if not isinstance(s, dict):
            continue
        sid = s.get("sentenceId") or s.get("id")
        text = s.get("text")
        if not isinstance(sid, str) or not isinstance(text, str) or not text.strip():
            continue
        sentences.append((sid, text[:MAX_SENTENCE_CHARS]))
    if not sentences:
        return {"groups": [], "fluff": []}

    candidates = _candidate_claims(db, list(raw_phrases) + _tokens(title))
    if not candidates:
        return {"groups": [], "fluff": []}

    # Prefilter: only sentences sharing a distinctive token with some candidate
    # are worth embedding. This is what bounds the embedding cost per request.
    cand_tokens: List[Tuple[int, List[str]]] = [(pid, _tokens(t)) for pid, t in candidates]
    vocabulary: Set[str] = {tk for _, toks in cand_tokens for tk in toks}
    kept: List[Tuple[str, str]] = []
    kept_tokens: List[Set[str]] = []
    for sid, text in sentences:
        toks = set(_tokens(text))
        if toks & vocabulary:
            kept.append((sid, text))
            kept_tokens.append(toks)
    if not kept:
        return {"groups": [], "fluff": []}

    lexical = _lexical_matches(kept_tokens, cand_tokens)
    vector = _vector_matches(db, [t for _, t in kept], [pid for pid, _ in candidates])

    # Every claim a sentence states, each at its more confident score; a
    # sentence stating several claims lands in each of their groups.
    matched = _merge_matches(lexical, vector)

    grouped: Dict[int, Dict[str, Any]] = {}
    for i in sorted(matched):
        for post_id, sim in matched[i]:
            g = grouped.setdefault(post_id, {"sentence_ids": [], "sim": 0.0})
            g["sentence_ids"].append(kept[i][0])
            g["sim"] = max(g["sim"], sim)

    cand_text = dict(candidates)
    groups: List[Dict[str, Any]] = []
    for post_id, g in grouped.items():
        summary = _summary(db, post_id)
        if not summary:
            # Created but not yet indexed — nothing to render a claim card from.
            continue
        groups.append({
            "group_id": f"oc:{post_id}",
            "canonical_text": summary.get("text") or cand_text.get(post_id, ""),
            "sentence_ids": g["sentence_ids"],
            "status": "mapped" if summary.get("is_active") else "low-liquidity",
            "match_score": g["sim"],
            "claim": summary,
        })

    logger.info(
        "locate: '%s' %d sentences -> %d candidates, %d kept, %d groups "
        "(lex=%d vec=%d)",
        title or "?", len(sentences), len(candidates), len(kept), len(groups),
        len(lexical), len(vector),
    )
    return {"groups": groups, "fluff": []}
