# app/chain/chain_db.py
"""
DB-backed chain reads. These replace direct RPC calls for API responses.
Data is populated by chain_indexer.py.

All functions accept a SQLAlchemy Session and return the same types
as the corresponding chain_reader.py functions.
"""

import logging
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)


def get_stake_totals(db: Session, post_id: int) -> tuple[float, float]:
    """Returns (support, challenge) from indexed DB."""
    row = db.execute(sql_text(
        "SELECT support_total, challenge_total FROM chain_post WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if row:
        return row[0], row[1]
    return 0.0, 0.0


def get_verity_score(db: Session, post_id: int) -> float:
    """Returns effective VS from indexed DB."""
    row = db.execute(sql_text(
        "SELECT effective_vs FROM chain_post WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if row:
        return row[0]
    return 0.0


def get_user_stake(db: Session, user_address: str, post_id: int, side: int) -> float:
    """Returns user's stake amount from indexed DB."""
    row = db.execute(sql_text(
        "SELECT amount FROM chain_user_stake "
        "WHERE user_address = :addr AND post_id = :pid AND side = :side"
    ), {"addr": user_address.lower(), "pid": post_id, "side": side}).fetchone()
    if row:
        return row[0]
    return 0.0


def get_user_lot_info(db: Session, user_address: str, post_id: int, side: int) -> dict | None:
    """Returns lot info from indexed DB."""
    row = db.execute(sql_text(
        "SELECT amount, weighted_position, entry_epoch, tranche, position_weight "
        "FROM chain_user_stake "
        "WHERE user_address = :addr AND post_id = :pid AND side = :side"
    ), {"addr": user_address.lower(), "pid": post_id, "side": side}).fetchone()
    if row and row[0] > 0:
        return {
            "amount": row[0],
            "weighted_position": row[1],
            "entry_epoch": row[2],
            "tranche": row[3],
            "position_weight": row[4],
        }
    return None


def get_post_info(db: Session, post_id: int) -> dict | None:
    """Returns full post info from indexed DB."""
    row = db.execute(sql_text(
        "SELECT post_id, content_type, creator, support_total, challenge_total, "
        "base_vs, effective_vs, is_active, created_epoch "
        "FROM chain_post WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if row:
        return {
            "post_id": row[0], "content_type": row[1], "creator": row[2],
            "support_total": row[3], "challenge_total": row[4],
            "base_vs": row[5], "effective_vs": row[6],
            "is_active": row[7], "created_epoch": row[8],
        }
    return None


def get_claim_text(db: Session, post_id: int) -> str | None:
    """Returns claim text from indexed DB."""
    row = db.execute(sql_text(
        "SELECT claim_text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    return row[0] if row else None


def get_global(db: Session, key: str) -> float | None:
    """Returns a global stat from indexed DB."""
    row = db.execute(sql_text(
        "SELECT value_num FROM chain_global WHERE key = :k"
    ), {"k": key}).fetchone()
    return row[0] if row else None


def get_all_posts(db: Session, limit: int = 500, include_links: bool = True) -> list[dict]:
    """Returns all indexed posts for Claims Explorer."""
    rows = db.execute(sql_text(
        "SELECT p.post_id, p.content_type, p.creator, p.created_epoch, "
        "p.support_total, p.challenge_total, p.base_vs, p.effective_vs, p.is_active, "
        "t.claim_text "
        "FROM chain_post p "
        "LEFT JOIN chain_claim_text t ON p.post_id = t.post_id "
        + ("WHERE p.content_type = 0 " if not include_links else "")
        + "ORDER BY (p.support_total + p.challenge_total) DESC "
        "LIMIT :lim"
    ), {"lim": limit}).fetchall()

    return [{
        "post_id": r[0], "content_type": r[1], "creator": r[2],
        "support_total": r[4], "challenge_total": r[5],
        "base_vs": r[6], "verity_score": r[7], "is_active": r[8],
        "text": r[9] or "", "created_epoch": r[3],
    } for r in rows]


def get_user_positions(db: Session, user_address: str) -> list[dict]:
    """Returns all staked positions for a user (for Portfolio)."""
    rows = db.execute(sql_text(
        "SELECT us.post_id, us.side, us.amount, us.tranche, us.position_weight, "
        "p.content_type, p.support_total, p.challenge_total, "
        "p.base_vs, p.effective_vs, p.is_active, "
        "COALESCE(t.claim_text, '') as claim_text, "
        "p.created_epoch, p.creator "
        "FROM chain_user_stake us "
        "JOIN chain_post p ON us.post_id = p.post_id "
        "LEFT JOIN chain_claim_text t ON us.post_id = t.post_id "
        "WHERE us.user_address = :addr AND us.amount > 0 "
        "ORDER BY us.amount DESC"
    ), {"addr": user_address.lower()}).fetchall()

    positions = []
    for r in rows:
        post_id = r[0]
        side = r[1]
        amount = r[2]
        tranche = r[3]
        pos_weight = r[4]
        content_type = r[5]
        support = r[6]
        challenge = r[7]
        base_vs = r[8]
        effective_vs = r[9]
        is_active = r[10]
        text = r[11]

        created_epoch = r[12] if len(r) > 12 else None
        creator = r[13] if len(r) > 13 else None
        from_post_id = None
        to_post_id = None
        is_challenge = None
        from_text_raw = None
        to_text_raw = None
        # For links, build descriptive text
        if content_type != 0 and not text:
            link_row = db.execute(sql_text(
                "SELECT l.from_post_id, l.to_post_id, l.is_challenge, "
                "ft.claim_text as from_text, tt.claim_text as to_text "
                "FROM chain_link l "
                "LEFT JOIN chain_claim_text ft ON l.from_post_id = ft.post_id "
                "LEFT JOIN chain_claim_text tt ON l.to_post_id = tt.post_id "
                "WHERE l.link_post_id = :pid"
            ), {"pid": post_id}).fetchone()
            if link_row:
                from_post_id = link_row[0]
                to_post_id = link_row[1]
                is_challenge = bool(link_row[2])
                from_text_raw = link_row[3] or ""
                to_text_raw = link_row[4] or ""
                verb = "challenges" if is_challenge else "supports"
                from_t = (from_text_raw or f"#{from_post_id}")[:30]
                to_t = (to_text_raw or f"#{to_post_id}")[:30]
                text = f'"{from_t}" {verb} "{to_t}"'

        # Determine winning/losing
        vs = effective_vs
        support_wins = vs > 0
        side_name = "support" if side == 0 else "challenge"
        is_winner = (side_name == "support" and support_wins) or \
                    (side_name == "challenge" and not support_wins)

        if vs == 0:
            status = "neutral"
        elif is_winner:
            status = "winning"
        else:
            status = "losing"

        # APR calculation (inline, no RPC).
        #
        # IMPORTANT: sMax is the global participation reference. Only
        # the largest active post (sMax post) gets participation=1.0;
        # smaller posts get participation = T/sMax < 1.0. We MUST NOT
        # fall back to the current post's own total — that would force
        # participation=1 for every post and pin every winning ±100%-VS
        # post to the rMax APR ceiling. (See git log: this was the bug
        # fixed by fix-apr-display.)
        total = support + challenge
        s_max_indexed = get_global(db, "s_max")
        if s_max_indexed and s_max_indexed > 0:
            s_max = s_max_indexed
        else:
            # Fallback: chain-wide max from chain_post. NOT this post's
            # own total. Use 1.0 as a numerical floor so we never divide
            # by zero, but log so we notice if the indexer is broken.
            row = db.execute(sql_text(
                "SELECT MAX(support_total + challenge_total) FROM chain_post"
            )).fetchone()
            s_max = max(float(row[0] or 0.0), 1.0)
            logger.warning(
                "chain_global.s_max missing or zero; falling back to "
                "chain-wide MAX(support+challenge)=%s. Indexer may be stale.",
                s_max,
            )

        # Rate policy from chain (indexed by chain_indexer). Defaults
        # match the deployed ProtocolPolicy: rMin=0, rMax=1.387611
        # (= 200% APR target compounded daily, i.e. ~100% to a sole
        # staker after the 0.5 midpoint position weight).
        R_MIN = get_global(db, 'rate_min_ray')
        R_MAX = get_global(db, 'rate_max_ray')
        if R_MIN is None: R_MIN = 0.0
        if R_MAX is None: R_MAX = 1.387611

        abs_vs = abs(vs)
        v = abs_vs / 100.0
        participation = min(total / s_max, 1.0) if s_max > 0 else 0.0
        r_base_annual = R_MIN + (R_MAX - R_MIN) * v * participation
        # Per-lot effective rate folds in the lot's positionWeight,
        # which the StakeEngine applies independently per lot. Sole
        # staker on a side has positionWeight=0.5; first of many
        # earlier stakers approaches positionWeight=1.
        r_daily = (r_base_annual / 365.24) * pos_weight if vs != 0 else 0.0
        compounded = ((1 + r_daily) ** 365.24 - 1) * 100 if r_daily > 0 else 0.0
        apr = compounded if is_winner else -compounded
        r_eff = r_daily * 365.24  # for breakdown display
        r_base = r_base_annual    # for breakdown display
        if vs == 0:
            apr = 0.0
            r_eff = 0.0
            r_base = 0.0

        positions.append({
            "post_id": post_id,
            "post_type": "link" if content_type != 0 else "claim",
            "is_link": content_type != 0,
            "text": text,
            "created_epoch": created_epoch,
            "creator": creator,
            "from_post_id": from_post_id,
            "to_post_id": to_post_id,
            "is_challenge": is_challenge,
            "from_text": from_text_raw,
            "to_text": to_text_raw,
            "user_support": amount if side == 0 else 0,
            "user_challenge": amount if side == 1 else 0,
            "user_total": amount,
            "user_net_side": side_name,
            "pool_support": support,
            "pool_challenge": challenge,
            "pool_total": total,
            "verity_score": effective_vs,
            "is_active": is_active,
            "position_status": status,
            "estimated_apr": round(apr, 1),
            "apr_breakdown": {
                "apr": round(apr, 1),
                "r_min": round(R_MIN * 100, 2),
                "r_max": round(R_MAX * 100, 2),
                "vs": round(vs, 2),
                "abs_vs": round(abs_vs, 2),
                "v": round(v, 4),
                "total_stake": round(total, 4),
                "s_max": round(s_max, 4),
                "participation": round(participation, 4),
                "r_base": round(r_base * 100, 2),
                "r_eff": round(r_eff * 100, 2),
                "position_weight": round(pos_weight, 3),
                "is_winner": is_winner,
                "is_smax_post": (total >= s_max - 1e-9),
            },
        })

    # Merge support + challenge on same post
    merged = {}
    for p in positions:
        pid = p["post_id"]
        if pid in merged:
            existing = merged[pid]
            existing["user_support"] += p["user_support"]
            existing["user_challenge"] += p["user_challenge"]
            existing["user_total"] = existing["user_support"] + existing["user_challenge"]
            if existing["user_support"] > 0 and existing["user_challenge"] > 0:
                existing["user_net_side"] = "both"
                existing["position_status"] = "hedged"
        else:
            merged[pid] = p

    return list(merged.values())


def get_edges(db: Session, post_id: int, direction: str) -> list[dict]:
    """Returns incoming or outgoing edges for a post."""
    if direction == "incoming":
        rows = db.execute(sql_text(
            "SELECT l.link_post_id, l.from_post_id, l.is_challenge, "
            "p.effective_vs, p.support_total, p.challenge_total, "
            "t.claim_text "
            "FROM chain_link l "
            "JOIN chain_post p ON l.from_post_id = p.post_id "
            "LEFT JOIN chain_claim_text t ON l.from_post_id = t.post_id "
            "WHERE l.to_post_id = :pid"
        ), {"pid": post_id}).fetchall()
        return [{
            "link_post_id": r[0], "claim_post_id": r[1],
            "is_challenge": r[2], "claim_vs": r[3],
            "claim_support": r[4], "claim_challenge": r[5],
            "claim_text": r[6] or "",
        } for r in rows]
    else:
        rows = db.execute(sql_text(
            "SELECT l.link_post_id, l.to_post_id, l.is_challenge, "
            "p.effective_vs, p.support_total, p.challenge_total, "
            "t.claim_text "
            "FROM chain_link l "
            "JOIN chain_post p ON l.to_post_id = p.post_id "
            "LEFT JOIN chain_claim_text t ON l.to_post_id = t.post_id "
            "WHERE l.from_post_id = :pid"
        ), {"pid": post_id}).fetchall()
        return [{
            "link_post_id": r[0], "claim_post_id": r[1],
            "is_challenge": r[2], "claim_vs": r[3],
            "claim_support": r[4], "claim_challenge": r[5],
            "claim_text": r[6] or "",
        } for r in rows]

def compute_edge_contribution(db: Session, target_post_id: int, link_post_id: int) -> float:
    """Per-link contribution ("Effect" column), in VSP.

    patch_game_b: read from the chain (ScoreEngine.getEdgeContribution) instead of mirroring
    the contract formula here — the mirror was the last off-chain copy of contract math and
    drifted the moment the formula (time-weighting, base-VS scale) changed. The `db` argument
    is kept for the call sites; it is unused. Raises on RPC failure (no fallback formula).
    """
    from chain.chain_reader import _get_score_engine
    from chain.vs import RAY
    se = _get_score_engine()
    contrib_wei = se.functions.getEdgeContribution(int(target_post_id), int(link_post_id)).call()
    return int(contrib_wei) / RAY
