# app/tx_log.py
"""
Tx-log helper: record, look up, and update entries in the tx_log table.

Used by:
  - /api/relay/async (relay.py) to record pending transactions on submission
  - chain_indexer.resolve_pending_txs to mark rows confirmed/reverted/dropped
  - /api/notifications/{address} (notifications.py) to query user history

All functions take an SQLAlchemy Session and assume the caller manages
the transaction. None of these helpers call db.commit() — the caller
decides when to commit.

Status transitions:
   inserted as → pending
   pending     → confirmed   (receipt.status == 1)
   pending     → reverted    (receipt.status == 0)
   pending     → dropped     (receipt not found after TX_PENDING_TIMEOUT)
"""

import logging
from typing import Optional
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def record_pending(
    db: Session,
    tx_hash: str,
    user_address: str,
    to_address: str,
    calldata: str,
    action_type: str,
    action_value: Optional[float] = None,
    fee_quote_vsp: Optional[float] = None,   # patch_relay_gas_fee: total shown to the user
    fee_charge_vsp: Optional[float] = None,  # patch_relay_gas_fee: what the relay collects after confirmation
) -> int:
    """Insert a tx_log row with status='pending'. Returns the new row id.

    tx_hash is stored as a lowercase hex string (with 0x prefix preserved
    if present). Idempotent via the unique constraint on tx_hash: if the
    same hash is recorded twice, returns the existing id.
    """
    txh = tx_hash.lower() if tx_hash.startswith("0x") else "0x" + tx_hash.lower()
    user = user_address.lower()
    to_addr = to_address.lower()
    calld = calldata if calldata.startswith("0x") else "0x" + calldata

    result = db.execute(sql_text("""
        INSERT INTO tx_log
            (tx_hash, user_address, to_address, calldata,
             action_type, action_value, status,
             fee_quote_vsp, fee_charge_vsp, fee_status)
        VALUES
            (:txh, :user, :to_addr, :calld, :atype, :aval, 'pending',
             :fq, :fc, :fs)
        ON CONFLICT (tx_hash) DO UPDATE
            SET tx_hash = EXCLUDED.tx_hash
        RETURNING id
    """), {
        "txh": txh, "user": user, "to_addr": to_addr, "calld": calld,
        "atype": action_type, "aval": action_value,
        # patch_relay_gas_fee: a positive charge is collected once the action confirms
        "fq": fee_quote_vsp, "fc": fee_charge_vsp,
        "fs": "pending" if (fee_charge_vsp or 0) > 0 else "none",
    })
    row_id = result.scalar()
    return row_id


def mark_confirmed(
    db: Session,
    tx_log_id: int,
    block_number: int,
    gas_used: int,
    post_id: Optional[int] = None,
) -> bool:
    """Mark a tx_log row as confirmed. Only updates if currently pending,
    so re-running is safe (idempotent — second call is a no-op).

    Returns True if the row was updated, False otherwise.
    """
    result = db.execute(sql_text("""
        UPDATE tx_log
        SET status = 'confirmed',
            block_number = :bn,
            gas_used = :gu,
            post_id = COALESCE(:pid, post_id),
            resolved_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND status = 'pending'
    """), {"id": tx_log_id, "bn": block_number, "gu": gas_used, "pid": post_id})
    return result.rowcount > 0


def mark_reverted(
    db: Session,
    tx_log_id: int,
    error_message: str,
    block_number: Optional[int] = None,
    gas_used: Optional[int] = None,
) -> bool:
    """Mark a tx_log row as reverted with an error message. Only updates if
    currently pending. Returns True if updated."""
    result = db.execute(sql_text("""
        UPDATE tx_log
        SET status = 'reverted',
            block_number = :bn,
            gas_used = :gu,
            error_message = :err,
            resolved_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND status = 'pending'
    """), {"id": tx_log_id, "bn": block_number, "gu": gas_used,
           "err": (error_message or "")[:1000]})
    return result.rowcount > 0


def mark_dropped(db: Session, tx_log_id: int) -> bool:
    """Mark a tx_log row as dropped (never mined within TX_PENDING_TIMEOUT).
    Only updates if currently pending. Returns True if updated."""
    result = db.execute(sql_text("""
        UPDATE tx_log
        SET status = 'dropped',
            error_message = 'Transaction not found on chain within timeout',
            resolved_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND status = 'pending'
    """), {"id": tx_log_id})
    return result.rowcount > 0


def get_pending_for_watcher(db: Session, min_age_seconds: int = 5,
                            limit: int = 50) -> list:
    """Return pending tx_log rows older than min_age_seconds, oldest first.

    The age filter avoids races with /api/relay/async's INSERT — we don't
    want to fetch a receipt before the tx has had time to propagate to
    the RPC's view of the mempool.

    Uses postgres-specific interval arithmetic for production. Tests
    that exercise this path may need to use a postgres test DB or
    bypass this function and call the marker helpers directly.
    """
    rows = db.execute(sql_text("""
        SELECT id, tx_hash, user_address, to_address, calldata,
               action_type, submitted_at,
               EXTRACT(EPOCH FROM (now() - submitted_at))::INT AS age_sec
        FROM tx_log
        WHERE status = 'pending'
          AND submitted_at < now() - (:age || ' seconds')::interval
        ORDER BY submitted_at
        LIMIT :lim
    """), {"age": min_age_seconds, "lim": limit}).fetchall()
    return rows


import base64 as _b64  # patch_bundle04_5_p2_unified_feed
import json as _json
from datetime import datetime as _datetime, timezone as _tz


def _encode_cursor(ts_unix: int, source: str, source_id: int) -> str:
    """Opaque pagination cursor. Client treats as a black box.
    Encodes [timestamp_unix, source_table, source_id] so the server
    can resume strictly earlier than this point."""
    payload = _json.dumps([int(ts_unix), str(source), int(source_id)], separators=(",", ":"))
    return _b64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str):
    """Return (ts_unix:int, source:str, source_id:int) or None if invalid.
    Invalid cursors do NOT raise — they're treated as 'no cursor' so a
    stale client doesn't take the API down."""
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = _b64.urlsafe_b64decode(cursor + pad).decode("ascii")
        ts, src, sid = _json.loads(raw)
        return int(ts), str(src), int(sid)
    except Exception:
        return None


def _ts_to_unix(v) -> int:
    """Datetime|int|None -> unix seconds (int). None -> 0."""
    if v is None:
        return 0
    if isinstance(v, _datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_tz.utc)
        return int(v.timestamp())
    try:
        return int(v)
    except Exception:
        return 0


def get_user_notifications(
    db: Session,
    user_address: str,
    pending_limit: int = 50,
    recent_limit: int = 50,
    before_cursor: Optional[str] = None,
) -> dict:
    """Unified-feed notifications for a user.

    pending: tx_log rows where status='pending'. Oldest first (FIFO).
    recent:  merged stream from three sources, newest first, capped at
             recent_limit AFTER merge & dedup:
               - tx_log         (relays we submitted, incl. reverts)
               - mm_trade       (MM buys/sells)
               - chain_tx       (per-event chain-sourced history,
                                 incl. transfers + protocol events)
             Dedup on tx_hash: chain_tx wins (authoritative timestamp
             via block_epoch), with tx_log enrichment (status,
             error_message, friendly action_type) folded in.
             Rows joined to chain_claim_text by post_id to populate
             a snippet field for display.
    next_cursor: opaque string; pass back as before_cursor to fetch
                 the next page. None if no more results.

    pending is NOT paginated (it's bounded by user activity).
    """
    user = user_address.lower()

    # ── pending: unchanged from prior behavior ──
    pending = db.execute(sql_text("""
        SELECT id, tx_hash, action_type, action_value, to_address,
               post_id, submitted_at
        FROM tx_log
        WHERE user_address = :user
          AND status = 'pending'
        ORDER BY submitted_at
        LIMIT :lim
    """), {"user": user, "lim": pending_limit}).fetchall()

    # ── recent: pull from three sources, fetch ~3x limit each so
    #            we have headroom to dedup and still return `limit` rows ──
    fetch_n = max(recent_limit * 3, 60)
    cur = _decode_cursor(before_cursor)

    # tx_log resolved rows
    if cur is None:
        txlog_rows = db.execute(sql_text("""
            SELECT t.id, t.tx_hash, t.action_type, t.action_value, t.to_address,
                   t.status, t.post_id, t.block_number, t.gas_used,
                   t.error_message, t.submitted_at, t.resolved_at,
                   cct.claim_text     AS claim_text,
                   cp.content_type    AS post_content_type,
                   cl_from.claim_text AS link_from_text,
                   cl_to.claim_text   AS link_to_text,
                   cl_edge.is_challenge AS link_is_challenge
                   -- patch_link_polarity_in_snippet: see chain_tx SELECT comment.
            FROM tx_log t
            LEFT JOIN chain_claim_text cct       ON cct.post_id     = t.post_id
            LEFT JOIN chain_post       cp        ON cp.post_id      = t.post_id
            LEFT JOIN chain_link       cl_edge   ON cl_edge.link_post_id = t.post_id
            LEFT JOIN chain_claim_text cl_from   ON cl_from.post_id = cl_edge.from_post_id
            LEFT JOIN chain_claim_text cl_to     ON cl_to.post_id   = cl_edge.to_post_id
            WHERE t.user_address = :user
              AND t.status IN ('confirmed','reverted','dropped')
            ORDER BY t.resolved_at DESC NULLS LAST
            LIMIT :lim
        """), {"user": user, "lim": fetch_n}).fetchall()
    else:
        # Cursor is (ts_unix, source, source_id). For tx_log, restrict
        # to rows older than ts (resolved_at < ts) or same-ts smaller id.
        ts, _src, _sid = cur
        txlog_rows = db.execute(sql_text("""
            SELECT t.id, t.tx_hash, t.action_type, t.action_value, t.to_address,
                   t.status, t.post_id, t.block_number, t.gas_used,
                   t.error_message, t.submitted_at, t.resolved_at,
                   cct.claim_text     AS claim_text,
                   cp.content_type    AS post_content_type,
                   cl_from.claim_text AS link_from_text,
                   cl_to.claim_text   AS link_to_text,
                   cl_edge.is_challenge AS link_is_challenge
                   -- patch_link_polarity_in_snippet: see chain_tx SELECT comment.
            FROM tx_log t
            LEFT JOIN chain_claim_text cct       ON cct.post_id     = t.post_id
            LEFT JOIN chain_post       cp        ON cp.post_id      = t.post_id
            LEFT JOIN chain_link       cl_edge   ON cl_edge.link_post_id = t.post_id
            LEFT JOIN chain_claim_text cl_from   ON cl_from.post_id = cl_edge.from_post_id
            LEFT JOIN chain_claim_text cl_to     ON cl_to.post_id   = cl_edge.to_post_id
            WHERE t.user_address = :user
              AND t.status IN ('confirmed','reverted','dropped')
              AND t.resolved_at IS NOT NULL
              AND EXTRACT(EPOCH FROM t.resolved_at) < :ts
            ORDER BY t.resolved_at DESC
            LIMIT :lim
        """), {"user": user, "ts": ts, "lim": fetch_n}).fetchall()

    # mm_trade rows
    if cur is None:
        mm_rows = db.execute(sql_text("""
            -- patch_bundle04_5_p33_txlog_select
            SELECT trade_id, side, qty_vsp, total_usdc, avg_price_usd,
                   created_at, tx_hash, fee_usdc
            FROM mm_trade
            WHERE lower(user_address) = :user
            ORDER BY created_at DESC
            LIMIT :lim
        """), {"user": user, "lim": fetch_n}).fetchall()
    else:
        ts, _src, _sid = cur
        mm_rows = db.execute(sql_text("""
            SELECT trade_id, side, qty_vsp, total_usdc, avg_price_usd,
                   created_at, tx_hash, fee_usdc
            FROM mm_trade
            WHERE lower(user_address) = :user
              AND EXTRACT(EPOCH FROM created_at) < :ts
            ORDER BY created_at DESC
            LIMIT :lim
        """), {"user": user, "ts": ts, "lim": fetch_n}).fetchall()

    # chain_tx rows, with LEFT JOIN to chain_claim_text for snippet
    if cur is None:
        ct_rows = db.execute(sql_text("""
            -- patch_bundle04_5_p32_link_joins
            SELECT ct.id, ct.block_number, ct.tx_hash, ct.log_index,
                   ct.block_epoch, ct.contract, ct.event_name,
                   ct.action_type, ct.counterparty,
                   ct.post_id, ct.amount_vsp, ct.is_challenge,
                   ct.principal_vsp, ct.fee_vsp,
                   ct.indexed_at, cct.claim_text,
                   cp.content_type AS post_content_type,
                   cl_from.claim_text AS link_from_text,
                   cl_to.claim_text   AS link_to_text,
                   cl_edge.is_challenge AS link_is_challenge
                   -- patch_link_polarity_in_snippet: surface link's own polarity for
                   -- snippet rendering. Distinct from ct.is_challenge
                   -- which is the ROW's polarity (e.g., for a stake,
                   -- the staker's stance, not the underlying link's).
            FROM chain_tx ct
            LEFT JOIN chain_claim_text cct       ON cct.post_id     = ct.post_id
            LEFT JOIN chain_post       cp        ON cp.post_id      = ct.post_id
            LEFT JOIN chain_link       cl_edge   ON cl_edge.link_post_id = ct.post_id
            LEFT JOIN chain_claim_text cl_from   ON cl_from.post_id = cl_edge.from_post_id
            LEFT JOIN chain_claim_text cl_to     ON cl_to.post_id   = cl_edge.to_post_id
            WHERE ct.user_address = :user
            ORDER BY COALESCE(ct.block_epoch, 0) DESC, ct.id DESC
            LIMIT :lim
        """), {"user": user, "lim": fetch_n}).fetchall()
    else:
        ts, _src, _sid = cur
        ct_rows = db.execute(sql_text("""
            SELECT ct.id, ct.block_number, ct.tx_hash, ct.log_index,
                   ct.block_epoch, ct.contract, ct.event_name,
                   ct.action_type, ct.counterparty,
                   ct.post_id, ct.amount_vsp, ct.is_challenge,
                   ct.principal_vsp, ct.fee_vsp,
                   ct.indexed_at, cct.claim_text,
                   cp.content_type AS post_content_type,
                   cl_from.claim_text AS link_from_text,
                   cl_to.claim_text   AS link_to_text,
                   cl_edge.is_challenge AS link_is_challenge
                   -- patch_link_polarity_in_snippet: surface link's own polarity for
                   -- snippet rendering. Distinct from ct.is_challenge
                   -- which is the ROW's polarity (e.g., for a stake,
                   -- the staker's stance, not the underlying link's).
            FROM chain_tx ct
            LEFT JOIN chain_claim_text cct       ON cct.post_id     = ct.post_id
            LEFT JOIN chain_post       cp        ON cp.post_id      = ct.post_id
            LEFT JOIN chain_link       cl_edge   ON cl_edge.link_post_id = ct.post_id
            LEFT JOIN chain_claim_text cl_from   ON cl_from.post_id = cl_edge.from_post_id
            LEFT JOIN chain_claim_text cl_to     ON cl_to.post_id   = cl_edge.to_post_id
            WHERE ct.user_address = :user
              AND COALESCE(ct.block_epoch, 0) < :ts
            ORDER BY COALESCE(ct.block_epoch, 0) DESC, ct.id DESC
            LIMIT :lim
        """), {"user": user, "ts": ts, "lim": fetch_n}).fetchall()

    # ── Build a unified item list with normalized fields ──
    # Each item carries: ts_unix, source, source_id, tx_hash, action_type,
    # status (when knowable), post_id, amount_vsp, counterparty,
    # gas_used, error_message, claim_text_snippet.
    items = []

    # Index tx_log rows by tx_hash (lowercased) for fast lookup when
    # merging with chain_tx — we want to enrich chain_tx with
    # tx_log.status / error_message when both exist for the same hash.
    txlog_by_hash = {}
    for r in txlog_rows:
        h = (r.tx_hash or "").lower()
        if h:
            txlog_by_hash[h] = r

    # Track tx_hashes claimed by chain_tx so we can skip tx_log dupes.
    chain_tx_hashes = set()

    # patch_notif_link_dedup: pre-scan ct_rows for tx_hashes whose set of events
    # includes an EdgeAdded. A createLink tx emits BOTH PostCreated (for
    # the link's own post-row) AND EdgeAdded (for the support/challenge
    # edge wired to the link's post). Both rows share the same tx_hash,
    # both pass the is_protocol_event gate, and both get enriched from
    # tx_log to action_type='link' — producing two identical UI rows
    # back-to-back. Only EdgeAdded carries the is_challenge boolean,
    # so it's the row we must keep; PostCreated for the link is
    # informationally redundant and gets skipped below. createClaim txs
    # are unaffected (no EdgeAdded → hash not in link_tx_hashes →
    # PostCreated row emits normally).
    link_tx_hashes = {
        (r.tx_hash or "").lower()
        for r in ct_rows
        if r.event_name == "EdgeAdded" and r.tx_hash
    }

    # patch_stake_event_semantics_txlog: fee attribution per tx_hash. The indexer
    # writes the same per-tx fee_vsp on every chain_tx row that belongs
    # to a multi-event tx (e.g. a polarity-flip setStake emits both
    # StakeWithdrawn AND StakeAdded; the indexer pulls fee from the
    # same fee_summary bucket for both, so both rows carry the full
    # fee). Pre-scan picks the row with the smallest id (== smallest
    # log_index = first event in the tx, skipping Patch I drops) as
    # the fee-bearing row. Other rows show fee_vsp=None.
    fee_attribution_id = {}
    for r in ct_rows:
        h = (r.tx_hash or "").lower()
        if not h or getattr(r, "fee_vsp", None) is None:
            continue
        # Same skip logic as the main loop: don't let a Patch I-dropped
        # PostCreated row claim the fee for a createLink tx.
        if r.event_name == "PostCreated" and h in link_tx_hashes:
            continue
        if h not in fee_attribution_id or r.id < fee_attribution_id[h]:
            fee_attribution_id[h] = r.id

    # chain_tx first (authoritative timestamp)
    for r in ct_rows:
        h = (r.tx_hash or "").lower()
        chain_tx_hashes.add(h)
        # patch_notif_link_dedup: skip the redundant PostCreated row for link txs.
        # The hash is recorded in chain_tx_hashes above so the tx_log
        # fallback branch below correctly recognizes the tx as
        # chain-covered and doesn't double-emit.
        if r.event_name == "PostCreated" and h in link_tx_hashes:
            continue
        enrich = txlog_by_hash.get(h)
        snippet = None
        ctext = getattr(r, "claim_text", None)
        if ctext:
            snippet = (ctext[:160] + "…") if len(ctext) > 160 else ctext
        # patch_bundle04_5_p21_enrich_gating
        # Only enrich rows where the chain_tx event is the canonical
        # protocol event for the action — NOT Transfer rows that may
        # share a tx_hash with the same relay. Otherwise a stake's
        # mechanical Transfer row inherits action_type='stake' and
        # the user sees three near-duplicate 'stake' rows.
        is_protocol_event = r.event_name in (
            "StakeAdded", "StakeWithdrawn", "PostCreated", "EdgeAdded"
        )
        # patch_stake_event_semantics_txlog: trust r.action_type (the indexer writes
        # the correct event-specific value: 'stake' for StakeAdded,
        # 'unstake' for StakeWithdrawn, 'claim' for PostCreated, 'link'
        # for EdgeAdded). The previous override clobbered StakeWithdrawn
        # rows in polarity-flip setStake txs, making them render as a
        # second 'stake' row instead of 'unstake'.
        action_type = r.action_type
        # gas_used and error_message: only attach to the canonical
        # protocol-event row (the one the user thinks of as 'the action').
        if is_protocol_event and enrich is not None:
            gas_used_val = int(enrich.gas_used) if enrich.gas_used is not None else None
            error_message_val = enrich.error_message
            status_val = enrich.status
        else:
            gas_used_val = None
            error_message_val = None
            status_val = "confirmed"
        items.append({
            "source":          "chain_tx",
            "source_id":       int(r.id),
            "ts_unix":         _ts_to_unix(r.block_epoch) or _ts_to_unix(r.indexed_at),
            "tx_hash":         r.tx_hash,
            "block_number":    int(r.block_number) if r.block_number is not None else None,
            "action_type":     action_type,
            "event_name":      r.event_name,
            "contract":        r.contract,
            "status":          status_val,
            "post_id":         int(r.post_id) if r.post_id is not None else None,
            "amount_vsp":      float(r.amount_vsp) if r.amount_vsp is not None else None,
            "counterparty":    r.counterparty,
            "is_challenge":    bool(r.is_challenge) if r.is_challenge is not None else None,
            "gas_used":        gas_used_val,
            "error_message":   error_message_val,
            "claim_snippet":   snippet,
            "principal_vsp":   float(r.principal_vsp) if getattr(r, "principal_vsp", None) is not None else None,
            # patch_stake_event_semantics_txlog: only the first row per tx_hash
            # carries the fee; see fee_attribution_id pre-scan above.
            "fee_vsp":         (float(r.fee_vsp)
                                  if (getattr(r, "fee_vsp", None) is not None
                                      and fee_attribution_id.get(h) == r.id)
                                  else None),
            # patch_bundle04_5_p32_link_fields
            "is_link_post":    (getattr(r, "post_content_type", None) == 1),
            "link_from_text":  getattr(r, "link_from_text", None),
            "link_to_text":    getattr(r, "link_to_text", None),
            # patch_link_polarity_in_snippet: link's own polarity (distinct from row's).
            "link_is_challenge": (bool(r.link_is_challenge)
                                   if getattr(r, "link_is_challenge", None) is not None
                                   else None),
        })

    # tx_log rows that did NOT get claimed by chain_tx (e.g. reverts —
    # no chain event was emitted, but tx_log knows it happened).
    for r in txlog_rows:
        h = (r.tx_hash or "").lower()
        if h in chain_tx_hashes:
            continue
        items.append({
            "source":          "tx_log",
            "source_id":       int(r.id),
            "ts_unix":         _ts_to_unix(r.resolved_at) or _ts_to_unix(r.submitted_at),
            "tx_hash":         r.tx_hash,
            "block_number":    int(r.block_number) if r.block_number is not None else None,
            "action_type":     r.action_type,
            "event_name":      None,
            "contract":        None,
            "status":          r.status,
            "post_id":         int(r.post_id) if r.post_id is not None else None,
            "amount_vsp":      float(r.action_value) if r.action_value is not None else None,
            "counterparty":    r.to_address,
            "is_challenge":    None,
            "gas_used":        int(r.gas_used) if r.gas_used is not None else None,
            "error_message":   r.error_message,
            "claim_snippet":   ((getattr(r, "claim_text", None) or "")[:160] + ("…" if len(getattr(r, "claim_text", None) or "") > 160 else "")) or None,
            "is_link_post":    (getattr(r, "post_content_type", None) == 1),
            "link_from_text":  getattr(r, "link_from_text", None),
            "link_to_text":    getattr(r, "link_to_text", None),
            # patch_link_polarity_in_snippet: link's own polarity. For reverted createLink,
            # chain_link won't have a row, so this is None and the
            # link-snippet branch in the FE doesn't fire (no
            # link_from_text/link_to_text either). Setting it for
            # consistency with the chain_tx item shape.
            "link_is_challenge": (bool(r.link_is_challenge)
                                   if getattr(r, "link_is_challenge", None) is not None
                                   else None),
        })

    # mm_trade rows. These don't carry a tx_hash today, so they can't
    # be deduped against chain_tx — accept as-is; the chain_tx side
    # would show as VSP transfer rows anyway, which is fine for the
    # "all my activity" view.
    for r in mm_rows:
        items.append({
            "source":          "mm_trade",
            "source_id":       int(r.trade_id),
            "ts_unix":         _ts_to_unix(r.created_at),
            "tx_hash":         getattr(r, "tx_hash", None),  # patch_bundle04_5_p33_txlog_item
            "block_number":    None,
            "action_type":     r.side,  # 'buy' | 'sell'
            "event_name":      None,
            "contract":        "MM",
            "status":          "confirmed",  # synchronous; always confirmed by the time the row exists
            "post_id":         None,
            "amount_vsp":      float(r.qty_vsp) if r.qty_vsp is not None else None,
            "counterparty":    None,
            "is_challenge":    None,
            "gas_used":        None,
            "error_message":   None,
            "claim_snippet":   None,
            "total_usdc":      float(r.total_usdc) if r.total_usdc is not None else None,
            "avg_price_usd":   float(r.avg_price_usd) if r.avg_price_usd is not None else None,
            "fee_usdc":        float(r.fee_usdc) if getattr(r, "fee_usdc", None) is not None else None,
        })

    # Sort newest-first by ts_unix, tiebreak by (source, source_id) DESC
    # for stable ordering. Then trim to recent_limit; compute cursor
    # from the last (oldest in page) item.
    items.sort(key=lambda x: (x["ts_unix"], x["source"], x["source_id"]),
               reverse=True)
    page = items[: recent_limit]
    next_cursor = None
    if len(items) > recent_limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["ts_unix"], last["source"], last["source_id"])

    def serialize_pending(r):
        return {
            "id":            int(r.id),
            "tx_hash":       r.tx_hash,
            "action_type":   r.action_type,
            "action_value":  float(r.action_value) if r.action_value is not None else None,
            "to_address":    r.to_address,
            "post_id":       int(r.post_id) if r.post_id is not None else None,
            "submitted_at":  r.submitted_at,
        }

    return {
        "address":     user,
        "pending":     [serialize_pending(r) for r in pending],
        "recent":      page,
        "next_cursor": next_cursor,
    }


# ── patch_relay_gas_fee: fee collection bookkeeping ─────────────────────────

def get_fees_due(db: Session, limit: int = 20, max_attempts: int = 5):
    """Confirmed actions whose relay fee has not been collected yet."""
    return db.execute(sql_text("""
        SELECT id, tx_hash, user_address, fee_charge_vsp, fee_attempts
        FROM tx_log
        WHERE fee_status = 'pending' AND status = 'confirmed'
          AND fee_charge_vsp > 0 AND fee_attempts < :ma
        ORDER BY resolved_at ASC NULLS LAST, id ASC
        LIMIT :lim
    """), {"lim": limit, "ma": max_attempts}).fetchall()


def waive_fees_for_failed(db: Session) -> int:
    """A reverted or dropped action costs the user nothing."""
    r = db.execute(sql_text("""
        UPDATE tx_log SET fee_status = 'waived'
        WHERE fee_status = 'pending' AND status IN ('reverted', 'dropped')
    """))
    return r.rowcount


def mark_fee_collected(db: Session, tx_log_id: int, fee_tx_hash: str, amount_vsp: float) -> bool:
    r = db.execute(sql_text("""
        UPDATE tx_log SET fee_status = 'collected', fee_tx_hash = :fh, fee_error = NULL
        WHERE id = :id AND fee_status = 'pending'
    """), {"id": tx_log_id, "fh": fee_tx_hash})
    if r.rowcount:
        db.execute(sql_text("""
            UPDATE relay_fee_log SET fee_charged_vsp = :amt
            WHERE tx_hash = (SELECT tx_hash FROM tx_log WHERE id = :id)
        """), {"amt": amount_vsp, "id": tx_log_id})
    return r.rowcount > 0


def mark_fee_attempt(db: Session, tx_log_id: int, error: str, final: bool) -> None:
    db.execute(sql_text("""
        UPDATE tx_log
        SET fee_attempts = fee_attempts + 1,
            fee_error = :err,
            fee_status = CASE WHEN :final THEN 'failed' ELSE fee_status END
        WHERE id = :id
    """), {"id": tx_log_id, "err": (error or "")[:300], "final": final})
