# app/claim_views.py
"""
Read-only API endpoints for claim summaries and evidence graph edges.
Used by the TopicExplorer frontend component.
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from web3 import Web3

from mm_wallet import w3
from config import PROTOCOL_VIEWS_ADDRESS, STAKE_ENGINE_ADDRESS
from chain.abi import STAKE_ENGINE_ABI
from chain.claim_summary import read_claim_summary
from db import get_db
from moderation import check_content_fast

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claims", tags=["claims"])

_RAY = 10**18


def _stake():
    if not STAKE_ENGINE_ADDRESS:
        raise HTTPException(503, "StakeEngine not configured")
    return w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
        abi=STAKE_ENGINE_ABI,
    )

def _score():
    from config import SCORE_ENGINE_ADDRESS
    from chain.abi import load_abi_optional
    if not SCORE_ENGINE_ADDRESS:
        raise HTTPException(503, "ScoreEngine not configured")
    abi = load_abi_optional("ScoreEngine")
    if not abi:
        raise HTTPException(503, "ScoreEngine ABI unavailable (run forge build in core/)")
    return w3.eth.contract(
        address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS),
        abi=abi,
    )


def _ray_to_pct(ray_value: int) -> float:
    return round(ray_value / _RAY * 100, 2)


def _wei_to_vsp(wei: int) -> float:
    return wei / 1e18


def _moderate_text(text: str) -> str:
    """Return text if clean, or a placeholder if blocked."""
    mod = check_content_fast(text)
    if mod.allowed:
        return text
    return "[Content hidden — policy violation]"


# ── /all must be defined BEFORE /{post_id} routes ──

@router.get("/all")
def all_claims(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """Deprecated: delegates to /fast/all (indexed DB, no RPC calls).
    Kept for backward compatibility with any external callers."""
    return claims_fast(limit=limit, db=db)



@router.get("/{post_id}/summary")
def claim_summary(post_id: int, db: Session = Depends(get_db)):
    """Claim summary from indexed DB."""
    from chain.chain_db import get_post_info, get_claim_text, get_edges
    info = get_post_info(db, post_id)
    if not info:
        raise HTTPException(404, f"Post {post_id} not found")
    text = get_claim_text(db, post_id) or ""
    incoming = get_edges(db, post_id, "incoming")
    outgoing = get_edges(db, post_id, "outgoing")
    total = info["support_total"] + info["challenge_total"]
    return {
        "post_id": post_id,
        "text": _moderate_text(text),
        "stake_support": round(info["support_total"], 4),
        "stake_challenge": round(info["challenge_total"], 4),
        "total_stake": round(total, 4),
        "posting_fee": 1.0,
        "is_active": info["is_active"],
        "base_vs": round(info["base_vs"], 2),
        "verity_score": round(info["effective_vs"], 2),
        "incoming_count": len(incoming),
        "outgoing_count": len(outgoing),
    }


@router.get("/{post_id}/live")
def claim_live(post_id: int):
    """Claim state read STRAIGHT FROM THE CHAIN, bypassing the indexed DB.

    /summary is derived from chain_* tables and so trails the chain by however
    long indexing takes. That lag is invisible almost everywhere, but it is
    glaring in the one moment a user is watching: right after they stake, the
    claim they just funded still reads as 0 staked and inactive. The extension
    polls this endpoint after a confirmed transaction so the UI reflects what
    the user just did.

    One ProtocolViews call, so every field is from the same block — no chance of
    stakes and score disagreeing. is_active comes from the contract's own
    totalStake >= postingFee test rather than being recomputed here.

    The struct is decoded from the raw return data by the field count the chain
    reports (9 since core v17, 10 before), not by the ABI artifact — a stale
    artifact used to shift the words by one and report incomingCount as
    effective_vs. See chain/claim_summary.py. A post has ONE score since core
    v17, so there is no base_vs here (the old response carried one).
    """
    if not PROTOCOL_VIEWS_ADDRESS:
        raise HTTPException(503, "ProtocolViews not configured")
    try:
        s = read_claim_summary(w3, PROTOCOL_VIEWS_ADDRESS, post_id)
    except Exception as e:
        logger.warning("claim_live(%d) failed: %s", post_id, e)
        raise HTTPException(502, "Chain read failed")

    return {
        "post_id": post_id,
        "support_vsp": round(_wei_to_vsp(s["supportStake"]), 6),
        "challenge_vsp": round(_wei_to_vsp(s["challengeStake"]), 6),
        "total_vsp": round(_wei_to_vsp(s["totalStake"]), 6),
        "posting_fee_vsp": round(_wei_to_vsp(s["postingFee"]), 6),
        "active": s["isActive"],
        "effective_vs": _ray_to_pct(s["effectiveVSRay"]),
    }


@router.get("/{post_id}/edges")
def claim_edges(post_id: int, db: Session = Depends(get_db)):
    """Evidence edges with contribution computed from indexed DB."""
    from chain.chain_db import get_edges, get_post_info, get_claim_text, compute_edge_contribution

    raw_incoming = get_edges(db, post_id, "incoming")
    raw_outgoing = get_edges(db, post_id, "outgoing")

    def enrich(edge: dict, target_post_id: int) -> dict:
        claim_pid = edge["claim_post_id"]
        link_pid = edge["link_post_id"]
        link_info = get_post_info(db, link_pid)
        link_total = (link_info["support_total"] + link_info["challenge_total"]) if link_info else 0
        link_vs = link_info["effective_vs"] if link_info else 0
        return {
            "claim_post_id": claim_pid,
            "link_post_id": link_pid,
            "is_challenge": edge["is_challenge"],
            "edge_contribution": compute_edge_contribution(db, target_post_id, link_pid),
            "claim_text": _moderate_text(edge.get("claim_text", "")),
            "claim_vs": round(edge.get("claim_vs", 0), 2),
            "claim_support": round(edge.get("claim_support", 0), 4),
            "claim_challenge": round(edge.get("claim_challenge", 0), 4),
            "link_support": round(link_info["support_total"], 4) if link_info else 0,
            "link_challenge": round(link_info["challenge_total"], 4) if link_info else 0,
            "link_vs": round(link_vs, 2),
        }

    return {
        "incoming": [enrich(e, post_id) for e in raw_incoming],
        "outgoing": [enrich(e, e["claim_post_id"]) for e in raw_outgoing],
    }



@router.get("/search")
def search_claims(q: str = "", limit: int = 50, include_links: bool = False, db: Session = Depends(get_db)):
    """Search all on-chain claims by text. Returns claims with metrics from indexed DB."""
    from chain.chain_db import get_all_posts, get_edges

    posts = get_all_posts(db, limit=500, include_links=include_links)

    # Filter by search query
    if q.strip():
        ql = q.lower()
        posts = [p for p in posts if ql in p["text"].lower() or ql in str(p["post_id"])]

    claims = []
    for p in posts[:limit]:
        incoming = get_edges(db, p["post_id"], "incoming")
        outgoing = get_edges(db, p["post_id"], "outgoing")

        total = p["support_total"] + p.get("challenge_total", 0)
        controversy = 0
        if total > 0:
            minority = min(p["support_total"], p.get("challenge_total", 0))
            controversy = minority / total

        topic_row = db.execute(sql_text(
            "SELECT ta.topic_key FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN topic_article ta ON sec.article_id = ta.article_id "
            "WHERE s.post_id = :pid LIMIT 1"
        ), {"pid": p["post_id"]}).fetchone()
        if not topic_row:
            topic_row = db.execute(sql_text(
                "SELECT topic FROM claim WHERE post_id = :pid AND topic IS NOT NULL LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()

        claims.append({
            "post_id": p["post_id"],
            "text": _moderate_text(p["text"]),
            "creator": p.get("creator", ""),
            "verity_score": round(p["verity_score"], 2),
            "stake_support": round(p["support_total"], 4),
            "stake_challenge": round(p.get("challenge_total", 0), 4),
            "total_stake": round(total, 4),
            "controversy": round(controversy, 4),
            "incoming_links": len(incoming),
            "outgoing_links": len(outgoing),
            "topic": topic_row[0] if topic_row else None,
        })

    return {"claims": claims, "total": len(claims)}

@router.get("/fast/all")
def claims_fast(limit: int = 500, include_links: bool = True, db: Session = Depends(get_db)):
    """Fast claims explorer using indexed DB data (no RPC calls).
    Returns both claims (content_type=0) and links (content_type=1)."""
    from chain.chain_db import get_all_posts, get_edges
    
    posts = get_all_posts(db, limit=limit, include_links=include_links)
    
    # Build a lookup of link metadata: link_post_id -> {from_post_id, to_post_id, is_challenge, from_text, to_text}
    link_meta = {}
    if include_links:
        link_rows = db.execute(sql_text(
            "SELECT l.link_post_id, l.from_post_id, l.to_post_id, l.is_challenge, "
            "       cf.claim_text, ct.claim_text "
            "FROM chain_link l "
            "LEFT JOIN chain_claim_text cf ON cf.post_id = l.from_post_id "
            "LEFT JOIN chain_claim_text ct ON ct.post_id = l.to_post_id"
        )).fetchall()
        for r in link_rows:
            link_meta[r[0]] = {
                "from_post_id": r[1], "to_post_id": r[2], "is_challenge": r[3],
                "from_text": r[4] or "", "to_text": r[5] or "",
            }
    
    claims = []
    for p in posts:
        # Count links
        incoming = get_edges(db, p["post_id"], "incoming")
        outgoing = get_edges(db, p["post_id"], "outgoing")
        
        total = p["support_total"] + p["challenge_total"]
        # New controversy: total stake × (1 - |VS|/100)
        # Captures both scale (more stake = more meaningful) and dispute level
        vs_abs = abs(p.get("verity_score", 0)) / 100.0
        controversy = total * (1 - vs_abs)
        
        # Find topic — patch_2a_readflip: claim_topic (m2m) primary first,
        # full set in topics_list; legacy paths below are the un-healed fallback.
        _ct = db.execute(sql_text(
            "SELECT t.label FROM claim_topic ct "
            "JOIN topic t ON t.topic_id = ct.topic_id "
            "JOIN claim c ON c.claim_id = ct.claim_id "
            "JOIN chain_claim_text x ON x.claim_text = c.claim_text "
            "WHERE x.post_id = :pid ORDER BY ct.is_primary DESC, t.label"
        ), {"pid": p["post_id"]}).fetchall()
        topics_list = [r[0] for r in _ct]
        topic_row = (topics_list[0],) if topics_list else None
        # patch_link_topic: links have no claim_topic of their own -> inherit the
        # target (then source) claim's m2m topics. Target primary shown first.
        if not topics_list:
            _lt = db.execute(sql_text(
                "SELECT t.label FROM chain_link l "
                "JOIN chain_claim_text x ON x.post_id IN (l.to_post_id, l.from_post_id) "
                "JOIN claim c ON c.claim_text = x.claim_text "
                "JOIN claim_topic ct ON ct.claim_id = c.claim_id "
                "JOIN topic t ON t.topic_id = ct.topic_id "
                "WHERE l.link_post_id = :pid "
                "ORDER BY (x.post_id = l.to_post_id) DESC, ct.is_primary DESC, t.label"
            ), {"pid": p["post_id"]}).fetchall()
            _seen = set()
            topics_list = [r[0] for r in _lt if not (r[0] in _seen or _seen.add(r[0]))]
            topic_row = (topics_list[0],) if topics_list else None
        if not topic_row:
            topic_row = db.execute(sql_text(
                "SELECT ta.topic_key FROM article_sentence s "
                "JOIN article_section sec ON s.section_id = sec.section_id "
                "JOIN topic_article ta ON sec.article_id = ta.article_id "
                "WHERE s.post_id = :pid LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()
        if not topic_row:
            topic_row = db.execute(sql_text(
                "SELECT topic FROM claim WHERE post_id = :pid AND topic IS NOT NULL LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()
        # APP-12: Links inherit topic from their target claim
        if not topic_row and p.get("content_type", 0) == 1:
            topic_row = db.execute(sql_text(
                "SELECT ta.topic_key FROM chain_link l "
                "JOIN article_sentence s ON s.post_id = l.to_post_id "
                "JOIN article_section sec ON s.section_id = sec.section_id "
                "JOIN topic_article ta ON sec.article_id = ta.article_id "
                "WHERE l.link_post_id = :pid LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()
            if not topic_row:
                topic_row = db.execute(sql_text(
                    "SELECT c.topic FROM chain_link l "
                    "JOIN claim c ON c.post_id = l.to_post_id "
                    "WHERE l.link_post_id = :pid AND c.topic IS NOT NULL LIMIT 1"
                ), {"pid": p["post_id"]}).fetchone()
        
        is_link = p.get("content_type", 0) == 1
        entry = {
            "post_id": p["post_id"],
            "content_type": p.get("content_type", 0),
            "is_link": is_link,
            "text": _moderate_text(p["text"]) if not is_link else "",
            "creator": p.get("creator", ""),
            "verity_score": round(p["verity_score"], 2),
            "base_vs": round(p.get("base_vs", 0), 2),
            "stake_support": round(p["support_total"], 4),
            "stake_challenge": round(p.get("challenge_total", 0), 4),
            "total_stake": round(total, 4),
            "controversy": round(controversy, 4),
            "incoming_links": len(incoming),
            "outgoing_links": len(outgoing),
            "topic": topic_row[0] if topic_row else None,
            "topics": topics_list if topics_list else ([topic_row[0]] if topic_row else []),  # patch_2a_readflip
            "created_at": None,
            "created_epoch": p.get("created_epoch"),
        }
        # PD-04: Dupe group info
        try:
            dg = db.execute(sql_text(
                "SELECT c.dupe_group_id, g.canonical_post_id, g.member_count, "
                "       g.total_support, g.total_challenge, g.aggregate_vs "
                "FROM chain_claim_text c "
                "LEFT JOIN claim_dupe_group g ON c.dupe_group_id = g.group_id "
                "WHERE c.post_id = :pid"
            ), {"pid": p["post_id"]}).fetchone()
            if dg and dg[0]:
                entry["dupe_group_id"] = dg[0]
                entry["dupe_canonical_post_id"] = dg[1]
                entry["dupe_member_count"] = dg[2]
                if dg[2] > 1:
                    # Use aggregate metrics for rollup display
                    entry["dupe_total_support"] = dg[3]
                    entry["dupe_total_challenge"] = dg[4]
                    entry["dupe_aggregate_vs"] = dg[5]
        except Exception:
            pass
        
        # Enrich link entries
        if is_link and p["post_id"] in link_meta:
            m = link_meta[p["post_id"]]
            entry["from_post_id"] = m["from_post_id"]
            entry["to_post_id"] = m["to_post_id"]
            entry["is_challenge"] = m["is_challenge"]
            entry["from_text"] = _moderate_text(m["from_text"])
            entry["to_text"] = _moderate_text(m["to_text"])
            # For sorting purposes, use the target claim text
            entry["text"] = entry["to_text"]
        
        claims.append(entry)
    
    total_stake = sum(c["total_stake"] for c in claims)
    avg_vs = sum(c["verity_score"] for c in claims) / len(claims) if claims else 0
    
    return {
        "claims": claims,
        "total": len(claims),
        "total_stake": round(total_stake, 2),
        "avg_vs": round(avg_vs, 2),
    }



@router.get("/{post_id}/dupe-group")
def claim_dupe_group(post_id: int, db: Session = Depends(get_db)):
    """Get the dupe group for a claim, with all members sorted by stake."""
    from dupe_groups import get_dupe_group
    group = get_dupe_group(db, post_id)
    if not group:
        return {"group_id": None, "members": [], "member_count": 1}
    return group


@router.get("/{post_id}/queue")
def claim_queue(post_id: int, db: Session = Depends(get_db)):
    """Get all stakers and their positions for a post."""
    rows = db.execute(sql_text(
        "SELECT user_address, side, amount, weighted_position, entry_epoch, tranche, position_weight "
        "FROM chain_user_stake WHERE post_id = :pid AND amount > 0 ORDER BY side, weighted_position"
    ), {"pid": post_id}).fetchall()
    
    support = []
    challenge = []
    for r in rows:
        entry = {
            "address": r[0],
            "amount": round(r[2], 4),
            "position": round(r[3], 4),
            "entry_epoch": r[4],
            "tranche": r[5],
            "position_weight": r[6],
        }
        if r[1] == 0:
            support.append(entry)
        else:
            challenge.append(entry)
    
    return {
        "post_id": post_id,
        "support": support,
        "challenge": challenge,
        "support_total": sum(s["amount"] for s in support),
        "challenge_total": sum(c["amount"] for c in challenge),
    }
