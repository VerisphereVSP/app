"""Operator takedown (patch_report_path).

A claim's text is on chain and cannot be removed; what the app can do is stop showing it. An admin hides a post
(``POST /api/admin/claims/{id}/hide``), every public view then renders the placeholder instead of the text, and
``unhide`` restores it. The set of hidden ids is read from the database and cached for a few seconds so the hot
``/api/claims/fast/all`` path does not pay a query per claim.

Reports arrive by e-mail (the frontend's Report link opens a mailto to abuse@ with the post id and text); the
decision is human; this module is only the switch.
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

HIDDEN_PLACEHOLDER = "[Content hidden — removed by Verisphere]"
_TTL_S = 5.0
_cache: tuple[float, frozenset[int]] = (0.0, frozenset())


def hidden_ids(db: Session, *, force: bool = False) -> frozenset[int]:
    """Post ids whose text is hidden by an admin; cached for _TTL_S seconds."""
    global _cache
    ts, ids = _cache
    now = time.monotonic()
    if not force and now - ts < _TTL_S:
        return ids
    try:
        rows = db.execute(sql_text("SELECT post_id FROM chain_claim_text WHERE admin_hidden")).fetchall()
        ids = frozenset(int(r[0]) for r in rows)
    except Exception:
        # before the migration, or a transient DB error: fail open on the cache, closed on nothing
        ids = ids if ts else frozenset()
    _cache = (now, ids)
    return ids


def invalidate() -> None:
    global _cache
    _cache = (0.0, _cache[1])


def mask(text: str, post_id: Optional[int], db: Session) -> str:
    """The placeholder if post_id is hidden, else text unchanged."""
    if post_id is not None and int(post_id) in hidden_ids(db):
        return HIDDEN_PLACEHOLDER
    return text


def set_hidden(db: Session, post_id: int, hidden: bool, reason: str | None) -> bool:
    """Flip the flag; returns False if the post has no indexed text row."""
    # CURRENT_TIMESTAMP rather than now(): the same statement runs on Postgres and on the SQLite test harness
    r = db.execute(sql_text(
        "UPDATE chain_claim_text SET admin_hidden = :h, "
        "admin_hidden_reason = CASE WHEN :h THEN :r ELSE NULL END, "
        "admin_hidden_at = CASE WHEN :h THEN CURRENT_TIMESTAMP ELSE NULL END "
        "WHERE post_id = :p"
    ), {"h": hidden, "r": (reason or "")[:500] or None, "p": int(post_id)})
    db.commit()
    invalidate()
    return (r.rowcount or 0) > 0
