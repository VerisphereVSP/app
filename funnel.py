# app/funnel.py — patch_funnel (v2 step 5a)
"""
The share-to-stake funnel as daily counts per step. What is kept is one integer per (day, step) — no visitor, no IP, no
wallet, no user agent, nothing that could be joined to a person. Counting never raises: a failed count is logged and the
request it rode on goes on.

  POST /api/funnel        {step}       — the browser reports a Start-sheet step (fixed list below; anything else is 400)
  GET  /api/funnel?days=N              — the last N days (default 14, max 90) per step, plus totals and the conversions

Server-side counts (no round trip): the share page (share_view, or share_preview when the user agent is a link-preview
bot), a side taken on the share page (side_taken), an invitation issued (invite_issued) and redeemed (invite_redeemed).
Browser-side counts, from the Start sheet: back_tap (the share page's button), start_open, start_wallet (a wallet is
connected — step 2 reached), start_funded (VSP in hand — step 3 reached), start_staked, start_skip.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_db

log = logging.getLogger("funnel")
router = APIRouter()

# the order is the funnel's order; GET reports it in this order
STEPS: tuple[str, ...] = (
    "share_preview",   # a chat app fetched the page to unfurl the link
    "share_view",      # a person opened the share page
    "side_taken",      # tapped Support / Challenge there
    "back_tap",        # tapped "Back it with VSP"
    "start_open",      # the Start sheet opened in the app
    "start_wallet",    # a wallet connected (or already was)
    "start_funded",    # VSP in the wallet (invitation claimed, or already held)
    "start_staked",    # the stake went through
    "start_skip",      # left the sheet before staking
    "invite_issued",
    "invite_redeemed",
)
BROWSER_STEPS = frozenset({"back_tap", "start_open", "start_wallet", "start_funded", "start_staked", "start_skip"})
PREVIEW_BOTS = ("whatsapp", "facebookexternalhit", "facebot", "twitterbot", "slackbot", "telegrambot", "discordbot",
                "linkedinbot", "skypeuripreview", "signal", "imessage", "googlebot", "bingbot", "applebot", "viber")


def is_preview_bot(user_agent: str | None) -> bool:
    ua = (user_agent or "").lower()
    return any(b in ua for b in PREVIEW_BOTS)


def count(db: Session, step: str, n: int = 1) -> None:
    """Add n to today's row for step. Never raises."""
    if step not in STEPS:
        return
    try:
        db.execute(sql_text(
            "INSERT INTO funnel_daily (day, step, n) VALUES (CURRENT_DATE, :s, :n) "
            "ON CONFLICT (day, step) DO UPDATE SET n = funnel_daily.n + :n"), {"s": step, "n": n})
        db.commit()
    except Exception as e:  # pragma: no cover — a count must never break the request it rides on
        try:
            db.rollback()
        except Exception:
            pass
        log.warning("funnel count failed for %s: %s", step, e)


class StepIn(BaseModel):
    step: str


@router.post("/api/funnel", status_code=204)
def funnel_post(body: StepIn, db: Session = Depends(get_db)):
    if body.step not in BROWSER_STEPS:
        raise HTTPException(400, "unknown step")
    count(db, body.step)
    return None


@router.get("/api/funnel")
def funnel_get(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    since = date.today() - timedelta(days=days - 1)
    rows = db.execute(sql_text("SELECT day, step, n FROM funnel_daily WHERE day >= :since ORDER BY day"), {"since": since}).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    totals = {s: 0 for s in STEPS}
    for day, step, n in rows:
        d = str(day)[:10]
        by_day.setdefault(d, {})[step] = int(n)
        if step in totals:
            totals[step] += int(n)
    def ratio(a: str, b: str) -> float | None:
        return round(totals[a] / totals[b], 3) if totals[b] else None
    return {
        "days": days, "since": since.isoformat(),
        "steps": list(STEPS),
        "by_day": [{"day": d, **{s: v.get(s, 0) for s in STEPS}} for d, v in sorted(by_day.items())],
        "totals": totals,
        "conversion": {  # each as a fraction of the step before it in the reader's path
            "view_to_side": ratio("side_taken", "share_view"),
            "side_to_back": ratio("back_tap", "side_taken"),
            "back_to_open": ratio("start_open", "back_tap"),
            "open_to_wallet": ratio("start_wallet", "start_open"),
            "wallet_to_funded": ratio("start_funded", "start_wallet"),
            "funded_to_staked": ratio("start_staked", "start_funded"),
            "view_to_staked": ratio("start_staked", "share_view"),
        },
    }
