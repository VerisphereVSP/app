# app/dupe_groups.py
"""
Semantic dupe group management.

Groups claims that make substantively the same assertion (cosine similarity >= 0.90).
Each group has a canonical claim (highest stake × VS effect).
All members must be similar to the canonical — if canonical changes, members
that don't match the new canonical are ejected.

Flow:
  1. New claim indexed → embed → compare against all group canonicals
  2. If similar to a canonical (>= DUPE_THRESHOLD), join that group
  3. If not similar to any, create a new singleton group
  4. Periodically re-evaluate canonicals (highest effect may change)
"""

import logging
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

# patch05: LLM-verified dedup
# Three-band similarity decision: >=HIGH (0.95) bundles directly,
# [LOW, HIGH) = [0.65, 0.95) asks the LLM for verification, <LOW skips.
# HIGH is deliberately tight (near-identical text only) so semantically
# opposite claims that share vocabulary get LLM-checked, not auto-merged.
HIGH_THRESHOLD = 0.95   # cosine similarity for direct bundling (no LLM call); raised 0.85->0.95 so opposites-with-shared-vocabulary (~0.91) fall into the LLM-verify band instead of auto-bundling
LOW_THRESHOLD  = 0.65   # cosine similarity floor for LLM verification
DUPE_THRESHOLD = HIGH_THRESHOLD  # legacy alias; preserved for any external callers


def _llm_verify_equivalent(db: Session, text_a: str, text_b: str,
                            post_a: int, post_b: int,
                            similarity: float) -> bool:
    """
    Ask the LLM whether two claims are semantically equivalent.
    Cached in claim_dupe_verdict keyed by (min(post_a,post_b), max(...)).
    Returns True iff EQUIVALENT. On any failure: returns False
    (conservative — don't bundle on uncertainty).
    """
    pa, pb = (post_a, post_b) if post_a < post_b else (post_b, post_a)
    ta, tb = (text_a, text_b) if post_a < post_b else (text_b, text_a)

    # Check cache first
    try:
        row = db.execute(sql_text(
            "SELECT verdict FROM claim_dupe_verdict "
            "WHERE post_a = :pa AND post_b = :pb"
        ), {"pa": pa, "pb": pb}).fetchone()
        if row:
            return row[0] == "EQUIVALENT"
    except Exception as e:
        # If the verdict table doesn't exist yet (migration not run),
        # log and fall through to the LLM call. Don't crash.
        logger.debug("verdict-cache lookup failed: %s", e)

    # Cache miss — ask the LLM
    try:
        from llm_provider import complete, MODEL as LLM_MODEL
    except Exception as e:
        logger.warning("LLM provider unavailable for dedup verify: %s", e)
        return False

    system = (
        "You decide whether two short factual claims express the same "
        "proposition. Two claims are EQUIVALENT if they make the same "
        "factual assertion — same subject, same predicate, same truth "
        "conditions — even if the wording differs. They are DIFFERENT "
        "if either could be true while the other is false, or if they "
        "make distinct assertions about distinct things. Answer with "
        "exactly one word: EQUIVALENT or DIFFERENT. No explanation."
    )
    prompt = f"Claim A: {ta!r}\nClaim B: {tb!r}\n\nAnswer (one word):"

    verdict = "DIFFERENT"  # conservative default
    try:
        raw = complete(prompt, system=system, max_tokens=10, temperature=0.0)
        token = (raw or "").strip().upper().split()[0] if raw else ""
        # Tolerate punctuation, partial matches
        if token.startswith("EQUIV"):
            verdict = "EQUIVALENT"
        elif token.startswith("DIFFER"):
            verdict = "DIFFERENT"
        else:
            logger.warning(
                "LLM dedup-verify: unparseable response %r for (%d, %d) sim=%.3f — "
                "defaulting to DIFFERENT",
                raw, pa, pb, similarity)
    except Exception as e:
        logger.warning("LLM dedup-verify call failed for (%d, %d): %s — "
                       "defaulting to DIFFERENT", pa, pb, e)
        # Don't cache failed calls — retry on next cycle
        return False

    # Cache the verdict (best-effort; tolerate if table missing)
    try:
        db.execute(sql_text(
            "INSERT INTO claim_dupe_verdict (post_a, post_b, verdict, similarity, model) "
            "VALUES (:pa, :pb, :v, :s, :m) "
            "ON CONFLICT (post_a, post_b) DO NOTHING"
        ), {"pa": pa, "pb": pb, "v": verdict, "s": float(similarity),
            "m": LLM_MODEL})
        db.commit()
    except Exception as e:
        logger.debug("verdict-cache write failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass

    logger.info("LLM dedup-verify: posts (%d,%d) sim=%.3f → %s",
                pa, pb, similarity, verdict)
    return verdict == "EQUIVALENT"


def _should_bundle(db: Session, this_pid: int, this_text: str,
                    candidate_pid: int, candidate_text: str,
                    distance: float) -> bool:
    """
    Three-band decision based on cosine distance:
      distance <= (1 - HIGH_THRESHOLD): bundle directly
      (1 - HIGH_THRESHOLD) < distance <= (1 - LOW_THRESHOLD): ask LLM
      distance > (1 - LOW_THRESHOLD): no
    """
    similarity = 1.0 - float(distance)
    if similarity >= HIGH_THRESHOLD:
        return True
    if similarity < LOW_THRESHOLD:
        return False
    # In the verification band — ask LLM
    return _llm_verify_equivalent(
        db, this_text, candidate_text, this_pid, candidate_pid, similarity)


def _fmt_vec(v: list) -> str:
    """Format a Python list as pgvector literal."""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _parse_vec(s) -> Optional[list]:
    """Parse pgvector text to list[float]."""
    if s is None:
        return None
    if isinstance(s, list):
        return s
    s = str(s).strip()
    if not s.startswith("["):
        return None
    return [float(x) for x in s[1:-1].split(",")]


def embed_claim(db: Session, post_id: int, claim_text: str) -> Optional[list]:
    """Embed a claim and store in chain_claim_text. Returns the embedding."""
    # Check if already embedded
    row = db.execute(sql_text(
        "SELECT embedding::text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()

    if row and row[0]:
        vec = _parse_vec(row[0])
        if vec:
            return vec

    # Compute embedding
    try:
        from embedding import embed
        vec = embed(claim_text)
    except Exception as e:
        logger.warning("Embedding failed for post %d: %s", post_id, e)
        return None

    # Store
    try:
        db.execute(sql_text(
            "UPDATE chain_claim_text SET embedding = (:v)::vector "
            "WHERE post_id = :pid"
        ), {"v": _fmt_vec(vec), "pid": post_id})
        db.commit()
    except Exception as e:
        logger.warning("Failed to store embedding for post %d: %s", post_id, e)
        db.rollback()

    return vec


def assign_to_group(db: Session, post_id: int) -> Optional[int]:
    """Assign a claim to its dupe group. Returns group_id."""
    # Get this claim's embedding
    row = db.execute(sql_text(
        "SELECT embedding::text, claim_text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()

    if not row or not row[0]:
        return None

    claim_vec = _parse_vec(row[0])
    claim_text = row[1]
    if not claim_vec:
        return None

    # Check if already in a group
    existing = db.execute(sql_text(
        "SELECT dupe_group_id FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if existing and existing[0]:
        return existing[0]

    # patch05: widen candidate window to LOW_THRESHOLD; gate via
    # _should_bundle which routes through the LLM if the candidate
    # falls in the verification band.
    widest_distance = 1.0 - LOW_THRESHOLD

    # Find best candidate AND fetch its claim text for the LLM check
    matches = db.execute(sql_text(
        "SELECT g.group_id, g.canonical_post_id, "
        "       (c.embedding <=> (SELECT embedding FROM chain_claim_text WHERE post_id = :pid)) as dist, "
        "       c.claim_text "
        "FROM claim_dupe_group g "
        "JOIN chain_claim_text c ON c.post_id = g.canonical_post_id "
        "WHERE c.embedding IS NOT NULL "
        "  AND g.canonical_post_id != :pid "
        "ORDER BY dist ASC "
        "LIMIT 1"
    ), {"pid": post_id}).fetchone()

    if matches and matches[2] is not None and matches[2] <= widest_distance \
            and _should_bundle(db, post_id, claim_text,
                                matches[1], matches[3], matches[2]):
        group_id = matches[0]
        # Join this group
        db.execute(sql_text(
            "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
        ), {"gid": group_id, "pid": post_id})
        # Update group stats
        _refresh_group_stats(db, group_id)
        db.commit()
        logger.info("Claim post_id=%d joined dupe group %d (dist=%.3f)",
                     post_id, group_id, matches[2])
        return group_id

    # No match — create new singleton group
    row = db.execute(sql_text(
        "INSERT INTO claim_dupe_group (canonical_post_id, canonical_text, member_count) "
        "VALUES (:pid, :txt, 1) RETURNING group_id"
    ), {"pid": post_id, "txt": claim_text}).fetchone()
    group_id = row[0]

    db.execute(sql_text(
        "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
    ), {"gid": group_id, "pid": post_id})
    _refresh_group_stats(db, group_id)
    db.commit()
    logger.info("Claim post_id=%d created new dupe group %d", post_id, group_id)
    return group_id


def _refresh_group_stats(db: Session, group_id: int):
    """Recompute canonical and aggregate VS.

    patch_vs_single_source: aggregate_vs is the stake-weighted mean of the members'
    chain effective_vs (chain.vs.stake_weighted_vs). Links are not an input:
    each member's effective_vs already contains its incoming evidence, and the
    old formula added VSP-denominated link mass to a base percentage.
    """
    members = db.execute(sql_text(
        "SELECT c.post_id, c.claim_text, "
        "       COALESCE(p.support_total, 0), COALESCE(p.challenge_total, 0), "
        "       COALESCE(p.effective_vs, 0) "
        "FROM chain_claim_text c "
        "LEFT JOIN chain_post p ON c.post_id = p.post_id "
        "WHERE c.dupe_group_id = :gid"
    ), {"gid": group_id}).fetchall()
    if not members:
        return

    best_pid, best_text, best_effect = members[0][0], members[0][1], 0.0
    total_sup = total_chal = 0.0

    for pid, text, sup, chal, vs in members:
        total_sup += sup; total_chal += chal
        eff = (sup + chal) * abs(vs) / 100.0 if vs != 0 else 0
        if eff > best_effect:
            best_effect, best_pid, best_text = eff, pid, text

    if best_effect == 0:
        for m in members:
            if m[2] + m[3] > (best_effect or 0):
                best_pid, best_text = m[0], m[1]
                best_effect = m[2] + m[3]

    from chain.vs import stake_weighted_vs
    agg_vs = stake_weighted_vs((vs, sup + chal) for _pid, _text, sup, chal, vs in members)

    db.execute(sql_text(
        "UPDATE claim_dupe_group SET "
        "canonical_post_id = :cpid, canonical_text = :ctxt, "
        "member_count = :mc, total_support = :ts, total_challenge = :tc, "
        "aggregate_vs = :avs, updated_at = NOW() WHERE group_id = :gid"
    ), {"cpid": best_pid, "ctxt": best_text, "mc": len(members),
        "ts": total_sup, "tc": total_chal, "avs": round(agg_vs, 2), "gid": group_id})

    # patch05a: fix ejection to use _should_bundle (three-band
    # LLM-verified gate), matching the merge decisions in
    # assign_to_group and refresh_all_groups. Without this, the
    # ejection check used the tight HIGH_THRESHOLD and immediately
    # tore apart any LLM-justified merge, producing empty groups
    # and orphaned posts.
    #
    # Verdicts are cached, so this only fires an LLM call once per
    # (canonical, member) pair. Repeated _refresh_group_stats calls
    # hit the cache and are effectively free.
    canonical_text = best_text or ""
    for m in members:
        if m[0] == best_pid:
            continue
        dist_row = db.execute(sql_text(
            "SELECT (c1.embedding <=> c2.embedding), c1.claim_text "
            "FROM chain_claim_text c1, chain_claim_text c2 "
            "WHERE c1.post_id = :p1 AND c2.post_id = :p2 "
            "AND c1.embedding IS NOT NULL AND c2.embedding IS NOT NULL"
        ), {"p1": m[0], "p2": best_pid}).fetchone()
        if not dist_row or dist_row[0] is None:
            continue
        distance = dist_row[0]
        member_text = dist_row[1] or ""
        # Eject iff _should_bundle says no
        if not _should_bundle(db, m[0], member_text,
                               best_pid, canonical_text, distance):
            db.execute(sql_text(
                "UPDATE chain_claim_text SET dupe_group_id = NULL WHERE post_id = :pid"
            ), {"pid": m[0]})
            logger.info("Ejected %d from group %d (dist=%.3f, not bundle-worthy)",
                        m[0], group_id, distance)
            _create_singleton_group(db, m[0])


def _create_singleton_group(db: Session, post_id: int):
    """Create a singleton group for an ejected claim."""
    text_row = db.execute(sql_text(
        "SELECT claim_text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    text = text_row[0] if text_row else ""

    row = db.execute(sql_text(
        "INSERT INTO claim_dupe_group (canonical_post_id, canonical_text, member_count) "
        "VALUES (:pid, :txt, 1) RETURNING group_id"
    ), {"pid": post_id, "txt": text}).fetchone()

    db.execute(sql_text(
        "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
    ), {"gid": row[0], "pid": post_id})


def get_dupe_group(db: Session, post_id: int) -> Optional[Dict[str, Any]]:
    """Get the dupe group for a claim, with all members."""
    group_row = db.execute(sql_text(
        "SELECT g.group_id, g.canonical_post_id, g.canonical_text, "
        "       g.member_count, g.total_support, g.total_challenge, g.aggregate_vs "
        "FROM claim_dupe_group g "
        "JOIN chain_claim_text c ON c.dupe_group_id = g.group_id "
        "WHERE c.post_id = :pid"
    ), {"pid": post_id}).fetchone()

    if not group_row:
        return None

    group_id = group_row[0]

    # Get all members
    members = db.execute(sql_text(
        "SELECT c.post_id, c.claim_text, "
        "       COALESCE(p.support_total, 0), COALESCE(p.challenge_total, 0), "
        "       COALESCE(p.effective_vs, 0) "
        "FROM chain_claim_text c "
        "LEFT JOIN chain_post p ON c.post_id = p.post_id "
        "WHERE c.dupe_group_id = :gid "
        "ORDER BY (COALESCE(p.support_total, 0) + COALESCE(p.challenge_total, 0)) DESC"
    ), {"gid": group_id}).fetchall()

    return {
        "group_id": group_id,
        "canonical_post_id": group_row[1],
        "canonical_text": group_row[2],
        "member_count": group_row[3],
        "total_support": group_row[4],
        "total_challenge": group_row[5],
        "aggregate_vs": group_row[6],
        "members": [{
            "post_id": m[0],
            "text": m[1],
            "support": m[2],
            "challenge": m[3],
            "verity_score": m[4],
            "is_canonical": m[0] == group_row[1],
        } for m in members],
    }


def sweep_unassigned_groups(db: Session) -> int:
    """patch_group_global: non-destructive periodic maintenance. Assign ONLY claims that
    are embedded but ungrouped (dupe_group_id IS NULL) via assign_to_group (global,
    incremental). NEVER deletes or re-shards existing groups — this replaces the old
    destructive per-topic rebuild on the worker's timer, and catches claims that missed
    incremental assignment (e.g. embedding was null at creation during an outage).

    Returns the number of claims newly assigned.
    """
    rows = db.execute(sql_text(
        "SELECT post_id FROM chain_claim_text "
        "WHERE embedding IS NOT NULL AND dupe_group_id IS NULL ORDER BY post_id"
    )).fetchall()
    n = 0
    for (pid,) in rows:
        try:
            if assign_to_group(db, pid):
                n += 1
        except Exception as e:
            logger.warning("sweep_unassigned_groups: assign failed for post %d: %s", pid, e)
    if n:
        logger.info("sweep_unassigned_groups: assigned %d previously-ungrouped claim(s)", n)

    # patch_canonical_election: canonical + aggregates are functions of LIVE stake and
    # must track it ("canonical can change"). Re-run the existing per-group refresher
    # (re-election + totals + aggregate_vs + bundling ejection) over every group each
    # cycle — this restores what the old destructive rebuild provided, without touching
    # membership. LLM cost bounded by the claim_dupe_verdict cache.
    refreshed = 0
    for (gid,) in db.execute(sql_text("SELECT group_id FROM claim_dupe_group ORDER BY group_id")).fetchall():
        try:
            _refresh_group_stats(db, gid)
            refreshed += 1
        except Exception as e:
            logger.warning("sweep: _refresh_group_stats failed for group %d: %s", gid, e)
    if refreshed:
        db.commit()
    return n


def refresh_all_groups(db: Session):
    """Rebuild claim dupe-groups from the UNIFIED grouping engine.

    unify-claim-groups: delegate to unified engine. The Claims view reads
    claim_dupe_group and the Article view reads group_topic; both now derive
    from unified_grouping with one threshold set (VSP_GROUP_THRESHOLD), so the
    two contexts show identical groupings. Per-topic, deterministic, idempotent.
    """
    try:
        from unified_grouping_db import rebuild_claim_dupe_groups
        n = rebuild_claim_dupe_groups(db)
        logger.info("refresh_all_groups (unified): rebuilt %d claim groups", n)
    except Exception as e:
        logger.warning("unified claim-group rebuild failed: %s", e)
        # do NOT fall back to legacy logic — leaving groups as-is is safer than
        # a divergent second authority recomputing them differently.
