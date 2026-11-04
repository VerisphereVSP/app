# app/activity.py — patch_activity
"""
What happened on your claims while you were away — the reason to come back.

For a wallet, the feed is what OTHER wallets did on the claims it holds a position on or created: stakes and
withdrawals with the VSP behind them, evidence linked for or against, the score before and after, a flip past the
half-way line called out; and, for a sponsor, invitations redeemed. Aggregated per claim, newest first — never one line
per stake.

Nothing new is stored. Everything is computed on read from tables that already hold wallets (chain_tx,
chain_user_stake, chain_post, chain_claim_text, chain_link, invite_code); the client keeps the cursor (the last chain_tx
id and the last time it looked), so the server never records who looked, or when.

  GET /api/activity/{address}?since=<chain_tx id>&since_ts=<ISO>&limit=N
      → {address, since, cursor, cursor_ts, claims: [...], invites: [...], count}
  GET /api/activity/{address}/count?since=&since_ts=
      → {count, cursor, cursor_ts}     — for badges

Without `since` (the first look) the feed covers the last FIRST_LOOK_DAYS days.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_db

log = logging.getLogger("activity")
router = APIRouter()

FIRST_LOOK_DAYS = 7
MAX_LIMIT = 50
SNIPPET_LEN = 160
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _addr(address: str) -> str:
    if not _ADDR.match(address or ""):
        raise HTTPException(400, "address must be a 0x-prefixed 40-hex-digit string")
    return address.lower()


def _share(support: float, challenge: float) -> Optional[float]:
    """Support's share of the stake, 0–100; None when nothing is staked."""
    tot = max(0.0, support) + max(0.0, challenge)
    if tot <= 0:
        return None
    return round(100.0 * max(0.0, support) / tot, 1)


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "since_ts must be an ISO-8601 timestamp")


def my_posts(db: Session, me: str) -> Dict[int, Dict[str, Any]]:
    """The claims this wallet holds a position on (role = its side, or both) or created (role = creator)."""
    out: Dict[int, Dict[str, Any]] = {}
    for pid, side, amt in db.execute(sql_text(
        "SELECT post_id, side, amount FROM chain_user_stake WHERE lower(user_address) = :me AND amount > 0"
    ), {"me": me}).fetchall():
        r = out.setdefault(int(pid), {"support": 0.0, "challenge": 0.0, "creator": False})
        r["challenge" if int(side) == 1 else "support"] += float(amt or 0)
    for (pid,) in db.execute(sql_text(
        "SELECT post_id FROM chain_post WHERE lower(creator) = :me AND content_type = 0"
    ), {"me": me}).fetchall():
        out.setdefault(int(pid), {"support": 0.0, "challenge": 0.0, "creator": False})["creator"] = True
    return out


def _role(r: Dict[str, Any]) -> str:
    if r["support"] > 0 and r["challenge"] > 0:
        return "both"
    if r["support"] > 0:
        return "support"
    if r["challenge"] > 0:
        return "challenge"
    return "creator"


def feed(db: Session, me: str, since: Optional[int], since_ts: Optional[datetime], limit: int, count_only: bool = False) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    cursor_row = db.execute(sql_text("SELECT COALESCE(MAX(id), 0) FROM chain_tx")).fetchone()
    cursor = int(cursor_row[0] or 0) if cursor_row else 0
    out: Dict[str, Any] = {"address": me, "since": since, "cursor": cursor, "cursor_ts": now.isoformat(), "claims": [], "invites": [], "count": 0}

    mine = my_posts(db, me)
    min_epoch = int(time.time()) - FIRST_LOOK_DAYS * 86400 if since is None else 0
    groups: Dict[int, Dict[str, Any]] = {}
    if mine:
        pids = sorted(mine)
        # one query; the post list is small (a person's positions), so an IN list is fine
        ph = ", ".join(f":p{i}" for i in range(len(pids)))
        params: Dict[str, Any] = {"me": me, "since": int(since or 0), "min_epoch": min_epoch}
        params.update({f"p{i}": p for i, p in enumerate(pids)})
        rows = db.execute(sql_text(f"""
            SELECT t.id, t.post_id, t.action_type, t.is_challenge, t.amount_vsp, t.block_epoch, t.user_address,
                   l.from_post_id, l.to_post_id
              FROM chain_tx t
              LEFT JOIN chain_link l ON l.link_post_id = t.post_id AND t.action_type = 'link'
             WHERE t.id > :since
               AND lower(t.user_address) <> :me
               AND t.action_type IN ('stake', 'unstake', 'link')
               AND (t.post_id IN ({ph}) OR l.to_post_id IN ({ph}) OR l.from_post_id IN ({ph}))
               AND COALESCE(t.block_epoch, 0) >= :min_epoch
             ORDER BY t.id
        """), params).fetchall()
        for tid, pid, at, ic, amt, be, _ua, lfrom, lto in rows:
            # a link's row carries the link post; file it under the claim of mine it touches
            target = int(pid)
            if at == "link":
                target = int(lto) if lto is not None and int(lto) in mine else (int(lfrom) if lfrom is not None and int(lfrom) in mine else int(pid))
                if target not in mine:
                    continue
            g = groups.setdefault(target, {"post_id": target, "support_n": 0, "challenge_n": 0, "support_vsp": 0.0, "challenge_vsp": 0.0,
                                           "unstake_n": 0, "unstake_vsp": 0.0, "links_n": 0, "links_challenge_n": 0,
                                           "wallets": set(), "latest_epoch": 0, "last_id": 0,
                                           "_d_support": 0.0, "_d_challenge": 0.0})
            a = float(amt or 0)
            if at == "stake":
                if ic:
                    g["challenge_n"] += 1; g["challenge_vsp"] += a; g["_d_challenge"] += a
                else:
                    g["support_n"] += 1; g["support_vsp"] += a; g["_d_support"] += a
            elif at == "unstake":
                g["unstake_n"] += 1; g["unstake_vsp"] += a
                if ic:
                    g["_d_challenge"] -= a
                else:
                    g["_d_support"] -= a
            else:
                g["links_n"] += 1
                if ic:
                    g["links_challenge_n"] += 1
            g["wallets"].add(_ua)
            g["latest_epoch"] = max(g["latest_epoch"], int(be or 0))
            g["last_id"] = max(g["last_id"], int(tid))

    claims: List[Dict[str, Any]] = []
    if groups and not count_only:
        pids = sorted(groups)
        ph = ", ".join(f":p{i}" for i in range(len(pids)))
        params = {f"p{i}": p for i, p in enumerate(pids)}
        posts = {int(r[0]): r for r in db.execute(sql_text(
            f"SELECT p.post_id, p.support_total, p.challenge_total, p.is_active, c.claim_text "
            f"FROM chain_post p LEFT JOIN chain_claim_text c ON c.post_id = p.post_id WHERE p.post_id IN ({ph})"
        ), params).fetchall()}
        for pid, g in groups.items():
            p = posts.get(pid)
            s_now = float(p[1] or 0) if p else 0.0
            c_now = float(p[2] or 0) if p else 0.0
            s_before = s_now - g["_d_support"]
            c_before = c_now - g["_d_challenge"]
            score_now = _share(s_now, c_now)
            score_before = _share(s_before, c_before)
            flipped = score_before is not None and score_now is not None and (score_before >= 50) != (score_now >= 50)
            text = (p[4] or "") if p else ""
            mine_r = mine[pid]
            claims.append({
                "post_id": pid,
                "text": (text[:SNIPPET_LEN] + "…") if len(text) > SNIPPET_LEN else text,
                "role": _role(mine_r),
                "your_vsp": round(mine_r["support"] + mine_r["challenge"], 4),
                "support_n": g["support_n"], "challenge_n": g["challenge_n"],
                "support_vsp": round(g["support_vsp"], 4), "challenge_vsp": round(g["challenge_vsp"], 4),
                "unstake_n": g["unstake_n"], "unstake_vsp": round(g["unstake_vsp"], 4),
                "links_n": g["links_n"], "links_challenge_n": g["links_challenge_n"],
                "wallets_n": len(g["wallets"]),
                "score_before": score_before, "score_now": score_now, "flipped": flipped,
                "latest_epoch": g["latest_epoch"], "last_id": g["last_id"],
            })
        claims.sort(key=lambda c: (c["latest_epoch"], c["last_id"]), reverse=True)
        claims = claims[:limit]

    # invitations redeemed since the last look (sponsor's view); wallet-to-wallet rows, nothing else
    ts_floor = since_ts if since_ts is not None else (now - timedelta(days=FIRST_LOOK_DAYS))
    inv_rows = db.execute(sql_text(
        "SELECT id, amount_wei, post_id, redeemed_at FROM invite_code "
        "WHERE lower(sponsor) = :me AND status = 'redeemed' AND redeemed_at > :floor ORDER BY redeemed_at DESC"
    ), {"me": me, "floor": ts_floor}).fetchall()
    invites = []
    for iid, wei, pid, rat in inv_rows:
        try:
            amount = float(int(str(wei))) / 1e18
        except (TypeError, ValueError):
            amount = 0.0
        invites.append({"id": int(iid), "amount_vsp": round(amount, 4), "post_id": int(pid) if pid is not None else None,
                        "redeemed_at": rat.isoformat() if hasattr(rat, "isoformat") else str(rat)})

    out["count"] = len(groups) + len(invites)
    if not count_only:
        out["claims"] = claims
        out["invites"] = invites
    return out


@router.get("/api/activity/{address}")
def get_activity(address: str, since: Optional[int] = Query(default=None, ge=0), since_ts: Optional[str] = None,
                 limit: int = Query(default=20, ge=1, le=MAX_LIMIT), db: Session = Depends(get_db)) -> Dict[str, Any]:
    return feed(db, _addr(address), since, _parse_ts(since_ts), limit)


@router.get("/api/activity/{address}/count")
def get_activity_count(address: str, since: Optional[int] = Query(default=None, ge=0), since_ts: Optional[str] = None,
                       db: Session = Depends(get_db)) -> Dict[str, Any]:
    r = feed(db, _addr(address), since, _parse_ts(since_ts), 1, count_only=True)
    return {"count": r["count"], "cursor": r["cursor"], "cursor_ts": r["cursor_ts"]}
