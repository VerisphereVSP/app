# app/chain_indexer.py
"""
Chain Indexer — atomic single-cursor poll loop with reorg confirmation buffer.

Runs as a background thread inside the worker process. Each cycle:

  1. Read chain head.
  2. Compute safe_head = chain_head - CONFIRMATION_DEPTH (reorg buffer).
  3. Compute [from_block, to_block] from the global cursor.
  4. Fetch events from all three contracts for that range (no DB writes).
  5. Sort events globally by (block, tx_index, log_index).
  6. Replay events into a transient effects map (affected posts, new links).
  7. Apply effects in one DB transaction; commit cursor with the rest.
  8. Resolve any pending tx_log rows.

If any step from (4)-(7) fails, the transaction rolls back and the cursor
stays at `last_block`. The next cycle retries the same range.

Tables populated (canonical state):
  chain_post         — per-post stake totals, VS, activity status
  chain_user_stake   — per-user per-post stake positions
  chain_link         — evidence links from LinkGraph
  chain_claim_text   — claim text from PostRegistry
  chain_global       — sMax and other global stats
  chain_indexer_state — last processed block (key='last_block_global')

Derived state (article system, dupe groups, topic detection) is decoupled
into derived_state_worker.py. We write to derived_state_queue here; that
worker reads and applies it.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from web3 import Web3
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_session_factory
from tx_signer import build_w3  # patch_bundle10_rpc_failover_p2
from config import RPC_READ_URLS
from config import (
    RPC_URL,
    POST_REGISTRY_ADDRESS,
    STAKE_ENGINE_ADDRESS,
    SCORE_ENGINE_ADDRESS,
    LINK_GRAPH_ADDRESS,
    VSP_TOKEN_ADDRESS,           # patch_bundle04_5_chain_tx_config_import
    MM_ADDRESS,
    COLD_SAFE_ADDRESSES,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

POLL_INTERVAL = 15  # seconds between cycles
BLOCK_BATCH   = 2000  # max blocks processed per cycle

# Reorg buffer: never process events from blocks within this many of head.
# 3 is conservative for Avalanche Fuji (effective finality ~1 block).
# For mainnet, raise to 5-10.
CONFIRMATION_DEPTH = int(os.environ.get("INDEXER_CONFIRMATION_DEPTH", "3"))

# Cold-start lookback when last_block_global is 0 (fresh DB).
COLD_START_LOOKBACK = 100_000

# APP-04: Sanitize on-chain claim text before DB insert.
MAX_CLAIM_DB_LENGTH = 5000

def _validate_claim_text(text: str) -> str:
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if ch == '\n' or ch == '\t' or (ord(ch) >= 32))
    if len(cleaned) > MAX_CLAIM_DB_LENGTH:
        cleaned = cleaned[:MAX_CLAIM_DB_LENGTH]
    return cleaned

# ── Web3 setup ─────────────────────────────────────────────────────

_w3 = None

def _get_w3():
    global _w3
    if _w3 is None:
        _w3 = build_w3(RPC_READ_URLS, require_connected=False)
    return _w3


def _load_abi(name):
    # Resolves in both Docker (/core mount) and bare local runs.
    from chain.abi import load_abi_optional
    return load_abi_optional(name) or []


# patch_bundle04_5_p21_load_abi_path
def _load_abi_paths(name, candidate_paths):
    """Try multiple paths in order, return the first ABI found.
    Used for contracts that may live in different build dirs
    (e.g. Forwarder lives in app/contracts/out, not /core/out).
    Returns [] if none of the candidates exist.
    """
    for p in candidate_paths:
        path = Path(p)
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)["abi"]
            except Exception as _e:
                logger.warning("ABI parse failed at %s: %s", p, _e)
                return []
    return []


def _get_contracts(w3):
    """Returns dict of contract_name -> (contract, [event_names_we_care_about])."""
    se = w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
        abi=_load_abi("StakeEngine"))
    reg = w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=_load_abi("PostRegistry"))
    lg = w3.eth.contract(
        address=Web3.to_checksum_address(LINK_GRAPH_ADDRESS),
        abi=_load_abi("LinkGraph"))
    # patch_bundle04_5_chain_tx_vsp_subscription
    contracts = {
        "StakeEngine":  (se,  ["StakeAdded", "StakeWithdrawn", "PostUpdated"]),
        "PostRegistry": (reg, ["PostCreated"]),
        "LinkGraph":    (lg,  ["EdgeAdded"]),
    }
    if VSP_TOKEN_ADDRESS:
        try:
            vsp_abi = _load_abi("VSPToken")
            if vsp_abi:
                vsp = w3.eth.contract(
                    address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
                    abi=vsp_abi)
                contracts["VSPToken"] = (vsp, ["Transfer"])
                logger.info("Indexer subscribed to VSPToken Transfer events at %s", VSP_TOKEN_ADDRESS)  # patch_bundle04_5_p2_vsp_success_log
            else:
                logger.warning("VSPToken ABI empty — Transfer events disabled")
        except Exception as _e:
            logger.warning("VSPToken ABI/contract load failed (Transfer events disabled): %s", _e)
    return contracts



# patch_bundle04_5_chain_tx_helper
_FORWARDER_TREASURY_CACHE = {"addr": None, "resolved": False}  # patch_bundle04_5_p2_treasury_filter

def _forwarder_treasury_address():
    """Read Forwarder.treasury() from chain once and cache it for
    the indexer process lifetime. Returns lowercase hex string or
    None if the Forwarder isn't deployed / ABI not present / RPC
    fails. A None result is cached too — we won't keep retrying
    on every poll. To re-discover, restart the worker container.
    """
    if _FORWARDER_TREASURY_CACHE["resolved"]:
        return _FORWARDER_TREASURY_CACHE["addr"]
    addr = None
    try:
        from config import FORWARDER_ADDRESS  # local import; avoid cycles at module load
        if FORWARDER_ADDRESS:
            _repo = Path(__file__).resolve().parent
            abi = _load_abi_paths("VerisphereForwarder", [
                "/core/out/VerisphereForwarder.sol/VerisphereForwarder.json",
                "/app/contracts/out/VerisphereForwarder.sol/VerisphereForwarder.json",
                # Bare local runs (no container mounts):
                str(_repo.parent / "core/out/VerisphereForwarder.sol/VerisphereForwarder.json"),
                str(_repo / "contracts/out/VerisphereForwarder.sol/VerisphereForwarder.json"),
            ])
            if abi:
                w3 = _get_w3()
                fwd = w3.eth.contract(
                    address=Web3.to_checksum_address(FORWARDER_ADDRESS),
                    abi=abi)
                addr = fwd.functions.treasury().call().lower()
                logger.info("Forwarder treasury (fee recipient) discovered: %s", addr)
            else:
                logger.info("Forwarder ABI not present at /core/out or /app/contracts/out — fee-recipient filter disabled")
        else:
            logger.info("FORWARDER_ADDRESS not configured — fee-recipient filter disabled")
    except Exception as _e:
        logger.warning("Forwarder.treasury() read failed (fee-recipient filter disabled): %s", _e)
    _FORWARDER_TREASURY_CACHE["addr"] = addr
    _FORWARDER_TREASURY_CACHE["resolved"] = True
    return addr


def _chain_tx_internal_addresses():
    """Return the set (lowercase) of addresses that should be filtered
    out of VSP Transfer chain_tx rows: protocol-internal sinks where
    transfers are mechanics of stake/MM operations, not user moves.

    Includes StakeEngine, MM_ADDRESS, Forwarder.treasury() (fee recipient,
    discovered from chain — patch_bundle04_5_p2), COLD_SAFE_ADDRESSES,
    and the zero address. Re-evaluated on each call so env changes
    (via --force-recreate) take effect; the Forwarder.treasury() read
    is cached after first successful call.
    """
    out = {"0x" + "0" * 40}
    if STAKE_ENGINE_ADDRESS:
        out.add(STAKE_ENGINE_ADDRESS.lower())
    if MM_ADDRESS:
        out.add(MM_ADDRESS.lower())
    treas = _forwarder_treasury_address()
    if treas:
        out.add(treas)
    # patch_bundle04_5_p22_protocol_contracts_internal
    # PostRegistry + LinkGraph: protocol contracts the user
    # interacts with via the Forwarder. user↔contract VSP
    # movements (claim/link bonds) are mechanics, not user
    # activity — mirror the existing StakeEngine treatment.
    if POST_REGISTRY_ADDRESS:
        out.add(POST_REGISTRY_ADDRESS.lower())
    if LINK_GRAPH_ADDRESS:
        out.add(LINK_GRAPH_ADDRESS.lower())
    for a in COLD_SAFE_ADDRESSES:
        out.add(a.lower())
    return out


_BLOCK_TS_CACHE = {}
_BLOCK_TS_CACHE_MAX = 4096

def _block_timestamp(w3, block_number: int):
    """Fetch and cache the timestamp (uint64 unix epoch) for a block.
    Returns None on RPC failure (so the chain_tx row still writes with
    NULL block_epoch — the row's existence matters more than the ts)."""
    cached = _BLOCK_TS_CACHE.get(block_number)
    if cached is not None:
        return cached
    try:
        ts = int(w3.eth.get_block(block_number).timestamp)
    except Exception as _e:
        logger.warning("block_timestamp(%d) failed: %s", block_number, _e)
        return None
    if len(_BLOCK_TS_CACHE) >= _BLOCK_TS_CACHE_MAX:
        # Crude eviction: drop the oldest ~25%. Not LRU but adequate;
        # the dict is small and this rarely fires.
        for k in list(_BLOCK_TS_CACHE.keys())[: _BLOCK_TS_CACHE_MAX // 4]:
            _BLOCK_TS_CACHE.pop(k, None)
    _BLOCK_TS_CACHE[block_number] = ts
    return ts


def _insert_chain_tx_row(db, row):  # patch_bundle04_5_p21_fee_summary
    """Insert a single chain_tx row. UNIQUE(tx_hash, log_index, user_address)
    means ON CONFLICT DO NOTHING is the idempotent path for the rare case
    where a manual backfill replays an already-indexed log.

    `row` may include principal_vsp and fee_vsp; both default to None
    so older callers (pre-2.1) don't need to change."""
    row = dict(row)  # don't mutate caller's dict
    row.setdefault("prin", None)
    row.setdefault("fee", None)
    db.execute(sql_text("""
        INSERT INTO chain_tx (
            block_number, tx_hash, log_index, block_epoch,
            contract, event_name, action_type,
            user_address, counterparty,
            post_id, amount_vsp, is_challenge,
            principal_vsp, fee_vsp
        ) VALUES (
            :bn, :txh, :li, :be,
            :ct, :en, :at,
            :ua, :cp,
            :pid, :amt, :ic,
            :prin, :fee
        )
        ON CONFLICT (tx_hash, log_index, user_address) DO NOTHING
    """), row)


def _compute_fee_summary_by_tx(all_events, internal_addrs):
    """For each tx_hash, sum Transfer values where the recipient is
    an internal address — these are mechanical fees/principal moves
    that we'll attribute to the protocol-event row(s) for that tx.

    Returns dict: tx_hash -> {
        'principal_to_stake_engine_vsp': float,
        'fee_to_other_internal_vsp':    float,
        'sender':                       lowercase address or None,
    }

    Convention:
      • Transfer to STAKE_ENGINE_ADDRESS  → principal (stake)
      • Transfer from STAKE_ENGINE_ADDRESS → principal (unstake)
      • Transfer to any other internal addr → fee
    The protocol-event row picks which side it represents:
      • StakeAdded     → principal_vsp = principal_to_stake_engine,
                          fee_vsp = fee_to_other_internal
      • StakeWithdrawn → principal_vsp = principal_from_stake_engine,
                          fee_vsp = fee_to_other_internal
      • PostCreated / EdgeAdded → fee_vsp only (no principal moves
                                    with claim/link creation today)
    """
    summary = {}
    se_addr = STAKE_ENGINE_ADDRESS.lower() if STAKE_ENGINE_ADDRESS else None
    for e in all_events:
        if e["event_name"] != "Transfer":
            continue
        evt = e["evt"]
        a = evt.args
        try:
            amt = float(a["value"]) / 1e18
        except Exception:
            continue
        try:
            f = a["from"].lower()
            t = a["to"].lower()
        except Exception:
            continue
        txh = evt.transactionHash.hex().lower() if hasattr(evt.transactionHash, "hex") else str(evt.transactionHash).lower()
        if not txh.startswith("0x"):
            txh = "0x" + txh
        bucket = summary.setdefault(txh, {
            "principal_to_se_vsp":   0.0,
            "principal_from_se_vsp": 0.0,
            "fee_to_other_internal_vsp": 0.0,
            "sender": None,
        })
        if se_addr and t == se_addr:
            bucket["principal_to_se_vsp"] += amt
            if bucket["sender"] is None and f not in internal_addrs:
                bucket["sender"] = f
        elif se_addr and f == se_addr:
            bucket["principal_from_se_vsp"] += amt
        elif t in internal_addrs:
            # patch_bundle04_5_p22_fee_sender_guard
            # Require sender external. Without this guard, an
            # internal-to-internal Transfer (e.g. PostRegistry
            # burning the claim bond by sending to 0x0) would be
            # counted as a user fee — but the user already paid
            # that VSP earlier in the same tx via the user→PostReg
            # Transfer, which is what really counts as their fee.
            if f not in internal_addrs:
                bucket["fee_to_other_internal_vsp"] += amt
                if bucket["sender"] is None:
                    bucket["sender"] = f
    return summary


def _record_chain_tx_for_event(db, w3, e, internal_addrs, fee_summary=None):
    """Write 1-2 chain_tx rows for a single indexed event.

    Protocol events: 1 row, user_address = the staker/creator.
    Transfer events: up to 2 rows (from-side transfer_out, to-side
    transfer_in), each suppressed if its side address is internal.
    """
    name = e["event_name"]
    contract = e["contract"]
    evt = e["evt"]
    bn = e["block"]
    txh = evt.transactionHash.hex().lower() if hasattr(evt.transactionHash, "hex") else str(evt.transactionHash).lower()
    if not txh.startswith("0x"):
        txh = "0x" + txh
    li = int(e["log_index"])
    be = _block_timestamp(w3, bn)

    if name == "StakeAdded":
        a = evt.args
        try:
            amt = float(a.amount) / 1e18 if hasattr(a, "amount") else None
        except Exception:
            amt = None
        # patch_stake_event_semantics_indexer: StakeAdded event emits `side` (uint8:
        # 0=support, 1=challenge), not `isChallenge`. The prior getattr
        # defaulted to False on every stake row.
        ic = bool(getattr(a, "side", 0) == 1)
        s = (fee_summary or {}).get(txh) or {}
        _insert_chain_tx_row(db, {
            "bn": bn, "txh": txh, "li": li, "be": be,
            "ct": contract, "en": name, "at": "stake",
            "ua": a.staker.lower(), "cp": None,
            "pid": int(a.postId), "amt": amt, "ic": ic,
            "prin": s.get("principal_to_se_vsp") or None,
            "fee":  s.get("fee_to_other_internal_vsp") or None,
        })
    elif name == "StakeWithdrawn":
        a = evt.args
        try:
            amt = float(a.amount) / 1e18 if hasattr(a, "amount") else None
        except Exception:
            amt = None
        # patch_stake_event_semantics_indexer: see StakeAdded comment above.
        ic = bool(getattr(a, "side", 0) == 1)
        s = (fee_summary or {}).get(txh) or {}
        _insert_chain_tx_row(db, {
            "bn": bn, "txh": txh, "li": li, "be": be,
            "ct": contract, "en": name, "at": "unstake",
            "ua": a.staker.lower(), "cp": None,
            "pid": int(a.postId), "amt": amt, "ic": ic,
            "prin": s.get("principal_from_se_vsp") or None,
            "fee":  s.get("fee_to_other_internal_vsp") or None,
        })
    elif name == "PostCreated":
        a = evt.args
        s = (fee_summary or {}).get(txh) or {}
        _insert_chain_tx_row(db, {
            "bn": bn, "txh": txh, "li": li, "be": be,
            "ct": contract, "en": name, "at": "claim",
            "ua": a.creator.lower(), "cp": None,
            "pid": int(a.postId), "amt": None, "ic": None,
            "prin": None,
            "fee":  s.get("fee_to_other_internal_vsp") or None,
        })
    elif name == "EdgeAdded":
        a = evt.args
        ic = bool(getattr(a, "isChallenge", False))
        link_pid = int(a.linkPostId)
        creator_row = db.execute(sql_text(
            "SELECT creator FROM chain_post WHERE post_id = :p"
        ), {"p": link_pid}).fetchone()
        if creator_row and creator_row[0]:
            s = (fee_summary or {}).get(txh) or {}
            _insert_chain_tx_row(db, {
                "bn": bn, "txh": txh, "li": li, "be": be,
                "ct": contract, "en": name, "at": "link",
                "ua": creator_row[0].lower(), "cp": None,
                "pid": link_pid, "amt": None, "ic": ic,
                "prin": None,
                "fee":  s.get("fee_to_other_internal_vsp") or None,
            })
    elif name == "Transfer":
        # patch_bundle04_5_p21_transfer_filter_tight
        a = evt.args
        # Standard ERC20: from, to, value
        try:
            amt = float(a["value"]) / 1e18 if "value" in a else float(a.value) / 1e18
        except Exception:
            amt = None
        try:
            from_addr = a["from"].lower()
        except Exception:
            from_addr = getattr(a, "from", "").lower()
        try:
            to_addr = a["to"].lower()
        except Exception:
            to_addr = getattr(a, "to", "").lower()

        # Option-C filter: suppress entire Transfer when EITHER side
        # is an internal address. Mechanical transfers (stake principal
        # to StakeEngine, relay fee to Forwarder.treasury, MM trade legs)
        # are folded into the protocol-event row's principal_vsp/fee_vsp
        # at write time (see fee_summary_by_tx in poll_events_atomic).
        # Genuine wallet-to-wallet transfers (faucet drops, gifts, sends)
        # survive — both sides external.
        if (from_addr in internal_addrs) or (to_addr in internal_addrs):
            return  # nothing written for this Transfer

        # transfer_out for from-side
        if from_addr:
            _insert_chain_tx_row(db, {
                "bn": bn, "txh": txh, "li": li, "be": be,
                "ct": contract, "en": name, "at": "transfer_out",
                "ua": from_addr, "cp": to_addr or None,
                "pid": None, "amt": amt, "ic": None,
            })
        # transfer_in for to-side
        if to_addr:
            _insert_chain_tx_row(db, {
                "bn": bn, "txh": txh, "li": li, "be": be,
                "ct": contract, "en": name, "at": "transfer_in",
                "ua": to_addr, "cp": from_addr or None,
                "pid": None, "amt": amt, "ic": None,
            })
    # Other event names (PostUpdated, etc.) intentionally produce
    # no chain_tx rows: they're state-recomputation triggers, not
    # user-visible actions.

# ── Canonical write helpers (no commits — caller manages transaction) ──

def _index_user_stake_canonical(db: Session, se, user_address: str, post_id: int):
    """Write a single user's stake position on a post. No commit."""
    addr = Web3.to_checksum_address(user_address)
    for side in (0, 1):
        try:
            lot_info = se.functions.getUserLotInfo(addr, post_id, side).call()
            # 2026-09-15: getUserLotInfo returns 4 fields since S-11 removed
            # entryEpoch — (amount, weightedPosition, sideTotal, positionWeight).
            # The old 5-field indexing raised IndexError on [4], swallowed at
            # debug level, so NO user position has been indexed since S-11.
            amount = lot_info[0] / 1e18
            weighted_pos = lot_info[1] / 1e18
            entry_epoch = lot_info[2] if len(lot_info) >= 5 else 0
            # patch_bundle04_5_p6_apr_pos_weight_off_by_one: positionWeight is lot_info[4], not [3].
            # StakeEngine.getUserLotInfo returns 5 values:
            #   0=amount, 1=weightedPosition, 2=entryEpoch,
            #   3=sideTotal, 4=positionWeight
            # Reading [3] mis-populated chain_user_stake.position_weight
            # with sideTotal (post-side total stake), producing absurd
            # APRs downstream via daily-compounded inflation.
            pos_weight = (lot_info[4] if len(lot_info) >= 5 else lot_info[3]) / 1e18

            if amount > 0:
                db.execute(sql_text("""
                    INSERT INTO chain_user_stake (user_address, post_id, side, amount, weighted_position,
                                                   entry_epoch, tranche, position_weight, indexed_at)
                    VALUES (:addr, :pid, :side, :amt, :wp, :ee, :tr, :pw, now())
                    ON CONFLICT (user_address, post_id, side) DO UPDATE SET
                        amount = :amt, weighted_position = :wp,
                        tranche = :tr, position_weight = :pw, indexed_at = now()
                """), {
                    "addr": user_address.lower(), "pid": post_id, "side": side,
                    "amt": amount, "wp": weighted_pos, "ee": entry_epoch,
                    "tr": 0, "pw": pos_weight,
                })
            else:
                db.execute(sql_text("""
                    DELETE FROM chain_user_stake
                    WHERE user_address = :addr AND post_id = :pid AND side = :side
                """), {"addr": user_address.lower(), "pid": post_id, "side": side})

        except Exception as e:
            logger.warning("Failed to index user stake %s post %d side %d: %s",
                         user_address[:10], post_id, side, e)


def _embed_claim_text(db: Session, post_id: int, claim_text: str) -> None:
    """Embed a newly indexed claim, so it is findable by vector search at once.

    Without this a claim carries a NULL embedding until some later pass fills it
    in, which means the extension's article matcher can only find it by literal
    token overlap — a paraphrase of a claim created a minute ago silently fails
    to match. Doing it here, in the same transaction as the text insert, makes
    "indexed" and "searchable" the same moment.

    Best-effort and non-committing: the caller owns the transaction, and an
    embedding-provider outage must never stall chain indexing.
    """
    try:
        row = db.execute(sql_text(
            "SELECT embedding IS NULL FROM chain_claim_text WHERE post_id = :pid"
        ), {"pid": post_id}).fetchone()
    except Exception:
        return
    # The upsert above nulls the vector whenever the text changes, so a non-null
    # one is current and costs nothing to keep.
    if row is None or not row[0]:
        return

    try:
        from embedding import embed
        vec = embed(claim_text)
    except Exception as e:
        logger.debug("Could not embed claim %d: %s", post_id, e)
        return

    try:
        db.execute(sql_text(
            "UPDATE chain_claim_text SET embedding = (:v)::vector WHERE post_id = :pid"
        ), {"v": "[" + ",".join(str(float(x)) for x in vec) + "]", "pid": post_id})
    except Exception as e:
        logger.debug("Could not store embedding for claim %d: %s", post_id, e)


def index_post_canonical(
    db: Session,
    post_id: int,
    user_addresses: list[str] | None = None,
) -> dict:
    """Write canonical chain state for a post to chain_post, chain_claim_text,
    chain_user_stake. Does NOT commit. Returns metadata about what was written.

    Returns {'content_type': 0|1, 'is_new': bool, 'claim_text': str|None}.
    Raises on failure — caller should rollback their transaction.
    """
    w3 = _get_w3()
    se_abi = _load_abi("StakeEngine")
    sc_abi = _load_abi("ScoreEngine")
    reg_abi = _load_abi("PostRegistry")
    se = w3.eth.contract(address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS), abi=se_abi)
    sc = w3.eth.contract(address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS), abi=sc_abi)
    reg = w3.eth.contract(address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=reg_abi)

    # ── Stake totals ─────
    support_wei, challenge_wei = se.functions.getPostTotals(post_id).call()
    support = support_wei / 1e18
    challenge = challenge_wei / 1e18
    total = support + challenge

    # ── VS scores ─────
    # patch_vs_single_source: the chain is the only source of a VS. A failed read
    # raises (this function is documented to raise; the caller rolls back)
    # instead of writing a fabricated 0.0 into chain_post.
    from chain.vs import read_effective_vs_pct
    effective_vs = read_effective_vs_pct(sc, post_id)
    base_vs = effective_vs  # patch_game_b: base VS is internal (v17 §4.1); a post has ONE score. Column kept for compatibility.

    # ── Post metadata ─────
    try:
        post_data = reg.functions.getPost(post_id).call()
        creator = post_data[0]
        content_type = post_data[2]
        created_epoch = post_data[1]
    except Exception:
        creator = None
        content_type = 0
        created_epoch = None

    is_active = total >= 1.0

    # Was this post already in chain_post before this call?
    existed = db.execute(sql_text(
        "SELECT 1 FROM chain_post WHERE post_id = :p"
    ), {"p": post_id}).fetchone()
    is_new = existed is None

    db.execute(sql_text("""
        INSERT INTO chain_post (post_id, content_type, creator, support_total, challenge_total,
                                base_vs, effective_vs, is_active, created_epoch, indexed_at)
        VALUES (:pid, :ct, :cr, :s, :c, :bvs, :evs, :active, :epoch, now())
        ON CONFLICT (post_id) DO UPDATE SET
            support_total = :s, challenge_total = :c,
            base_vs = :bvs, effective_vs = :evs,
            is_active = :active, indexed_at = now()
    """), {
        "pid": post_id, "ct": content_type, "cr": creator,
        "s": support, "c": challenge,
        "bvs": base_vs, "evs": effective_vs,
        "active": is_active, "epoch": created_epoch,
    })

    claim_text = None
    if content_type == 0:
        try:
            content_id = post_data[3]
            claim_text = _validate_claim_text(reg.functions.getClaim(content_id).call())
            # Display-side moderation flag is best-effort; default False.
            try:
                from moderation import check_content_fast
                is_moderated = not check_content_fast(claim_text).allowed
            except Exception:
                is_moderated = False
            db.execute(sql_text("""
                INSERT INTO chain_claim_text (post_id, claim_text, indexed_at)
                VALUES (:pid, :txt, now())
                ON CONFLICT (post_id) DO UPDATE SET
                    claim_text = :txt, is_moderated = :mod, indexed_at = now(),
                    -- An embedding describes the text it was computed from, so
                    -- edited text invalidates it. NULL means "needs embedding".
                    embedding = CASE
                        WHEN chain_claim_text.claim_text IS DISTINCT FROM EXCLUDED.claim_text
                        THEN NULL ELSE chain_claim_text.embedding END
            """), {"pid": post_id, "txt": claim_text, "mod": is_moderated})
        except Exception as e:
            logger.debug("Could not index claim text for post %d: %s", post_id, e)
            claim_text = None

    if claim_text:
        _embed_claim_text(db, post_id, claim_text)

    # User positions
    if user_addresses:
        for addr in user_addresses:
            _index_user_stake_canonical(db, se, addr, post_id)

    return {"content_type": content_type, "is_new": is_new, "claim_text": claim_text}


def index_link_canonical(db: Session, link_post_id: int, from_post_id: int,
                         to_post_id: int, is_challenge: bool):
    """Insert/update an evidence link. No commit."""
    db.execute(sql_text("""
        INSERT INTO chain_link (link_post_id, from_post_id, to_post_id, is_challenge, indexed_at)
        VALUES (:lpid, :fpid, :tpid, :ic, now())
        ON CONFLICT (link_post_id) DO UPDATE SET
            from_post_id = :fpid, to_post_id = :tpid,
            is_challenge = :ic, indexed_at = now()
    """), {"lpid": link_post_id, "fpid": from_post_id, "tpid": to_post_id, "ic": is_challenge})


def index_global_stats_canonical(db: Session):
    """Update chain_global stats. No commit. Best-effort within the transaction."""
    w3 = _get_w3()
    se = w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
        abi=_load_abi("StakeEngine"))

    try:
        s_max_wei = se.functions.sMax().call()
        s_max = s_max_wei / 1e18
        db.execute(sql_text("""
            INSERT INTO chain_global (key, value_num, updated_at)
            VALUES ('s_max', :val, now())
            ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
        """), {"val": s_max})

        try:
            decay_rate = se.functions.sMaxDecayRateRay().call()
            db.execute(sql_text("""
                INSERT INTO chain_global (key, value_num, updated_at)
                VALUES ('smax_decay_rate_ray', :val, now())
                ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
            """), {"val": decay_rate / 1e18})
        except Exception:
            pass
        try:
            decay_max = se.functions.sMaxDecayMaxEpochs().call()
            db.execute(sql_text("""
                INSERT INTO chain_global (key, value_num, updated_at)
                VALUES ('smax_decay_max_epochs', :val, now())
                ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
            """), {"val": decay_max})
        except Exception:
            pass

        try:
            rp = se.functions.protocolPolicy().call()
            rp_contract = w3.eth.contract(address=rp, abi=[
                {"inputs":[],"name":"stakeIntRateMinRay","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
                {"inputs":[],"name":"stakeIntRateMaxRay","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
            ])
            r_min_ray = rp_contract.functions.stakeIntRateMinRay().call()
            r_max_ray = rp_contract.functions.stakeIntRateMaxRay().call()
            db.execute(sql_text("""
                INSERT INTO chain_global (key, value_num, updated_at)
                VALUES ('rate_min_ray', :val, now())
                ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
            """), {"val": r_min_ray / 1e18})
            db.execute(sql_text("""
                INSERT INTO chain_global (key, value_num, updated_at)
                VALUES ('rate_max_ray', :val, now())
                ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
            """), {"val": r_max_ray / 1e18})
        except Exception:
            pass
    except Exception as e:
        logger.warning("Failed to read global stats: %s", e)
        raise


# ── Derived-state enqueue ──────────────────────────────────────────

def enqueue_derived_state(db: Session, post_id: int, is_new: bool):
    """Write a derived_state_queue row for this post. Caller commits.

    patch_bundle04_p2: ON CONFLICT DO NOTHING relies on the partial
    UNIQUE index uq_dsq_active_per_post (migration 211) so duplicate
    enqueues for the same (post_id, queue_kind) while an existing row
    is still pending/in_progress are silently dropped. The derived
    work is idempotent — one pending row covers all events on a post
    until processed."""
    kind = "post_create" if is_new else "post_update"
    db.execute(sql_text("""
        INSERT INTO derived_state_queue (post_id, queue_kind, status)
        VALUES (:pid, :kind, 'pending')
        -- patch_bundle04_p2_hotfix: ON CONFLICT ON CONSTRAINT works only for
        -- named constraints; uq_dsq_active_per_post is a partial UNIQUE INDEX,
        -- so we use the inference form (columns + WHERE predicate matching
        -- the partial-index definition).
        ON CONFLICT (post_id, queue_kind) WHERE status IN ('pending', 'in_progress') DO NOTHING
    """), {"pid": post_id, "kind": kind})


# ── Cursor management ─────────────────────────────────────────────

CURSOR_KEY = "last_block_global"

def _get_last_block(db: Session) -> int:
    row = db.execute(sql_text(
        "SELECT value FROM chain_indexer_state WHERE key = :k"
    ), {"k": CURSOR_KEY}).fetchone()
    return int(row[0]) if row else 0


def _set_last_block(db: Session, block: int):
    """No commit — caller manages transaction."""
    db.execute(sql_text("""
        INSERT INTO chain_indexer_state (key, value, updated_at)
        VALUES (:k, :v, now())
        ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()
    """), {"k": CURSOR_KEY, "v": str(block)})


# ── Connected-post expansion ───────────────────────────────────────

def _list_connected_posts(db: Session, post_id: int) -> set:
    """Return post_ids one hop away from this post via chain_link.
    Used for VS recomputation: a new edge into post X means posts that
    link to X also have their VS affected."""
    # patch_game_b: a change on a post affects every DESCENDANT's effective score (v17 §4.2),
    # not just one hop. Walk to_post_id edges recursively (bounded depth 12), plus the direct
    # parents and the incident link posts as before.
    rows = db.execute(sql_text(
        "WITH RECURSIVE down(pid, depth) AS ("
        "  SELECT to_post_id, 1 FROM chain_link WHERE from_post_id = :pid "
        "  UNION "
        "  SELECT l.to_post_id, d.depth + 1 FROM chain_link l JOIN down d ON l.from_post_id = d.pid "
        "  WHERE d.depth < 12"
        ") "
        "SELECT pid FROM down "
        "UNION "
        "SELECT DISTINCT from_post_id AS pid FROM chain_link WHERE to_post_id = :pid "
        "UNION "
        "SELECT DISTINCT link_post_id AS pid FROM chain_link "
        "WHERE from_post_id = :pid OR to_post_id = :pid"
    ), {"pid": post_id}).fetchall()
    return {row[0] for row in rows if row[0] != post_id}

def _apply_stake_deltas_best_effort(db: Session, post_ids: list) -> None:
    """patch_bundle04_followup: patch cached article JSON with fresh
    stake/VS values for the given posts. Cheap (no LLM calls, just
    DB writes); kept in the indexer poll loop rather than the
    derived-state worker since it's needed promptly for UI freshness.

    Best-effort: individual failures logged at DEBUG; outer try wraps
    the whole batch."""
    try:
        from articles.article_store import apply_stake_delta
        from chain.chain_reader import get_stake_totals, get_verity_score
    except Exception as e:
        logger.debug("apply_stake_delta unavailable: %s", e)
        return
    for pid in post_ids:
        try:
            s, ch = get_stake_totals(pid)
            vs = get_verity_score(pid)
            apply_stake_delta(db, pid, s, ch, vs)
        except Exception as e:
            logger.debug("apply_stake_delta(%d) failed: %s", pid, e)





# ── Atomic poll loop ──────────────────────────────────────────────

def poll_events_atomic(db: Session) -> dict:
    """Run one atomic poll cycle.

    Returns a stats dict; raises only on RPC fetch failure (rare; outer
    loop catches and continues). DB-write failures are caught here and
    cause the cursor to stay at last_block.
    """
    w3 = _get_w3()
    current_block = w3.eth.block_number
    safe_head = current_block - CONFIRMATION_DEPTH

    last_block = _get_last_block(db)
    if last_block == 0:
        last_block = max(safe_head - COLD_START_LOOKBACK, 0)
        logger.info("Indexer cold start: first poll from block %d (head=%d, safe=%d)",
                    last_block, current_block, safe_head)

    from_block = last_block + 1
    to_block = min(from_block + BLOCK_BATCH - 1, safe_head)
    if from_block > to_block:
        return {"events": 0, "posts": 0, "from_block": from_block, "to_block": from_block - 1}

    # ── Phase 1: fetch all events from chain (idempotent, no DB writes) ──
    contracts = _get_contracts(w3)
    all_events = []
    try:
        for contract_name, (contract, event_names) in contracts.items():
            for event_name in event_names:
                event_obj = getattr(contract.events, event_name)
                for evt in event_obj.get_logs(from_block=from_block, to_block=to_block):
                    all_events.append({
                        "block":      evt.blockNumber,
                        "tx_index":   evt.transactionIndex,
                        "log_index":  evt.logIndex,
                        "contract":   contract_name,
                        "event_name": event_name,
                        "evt":        evt,
                    })
    except Exception as e:
        logger.warning("RPC fetch failed for range %d-%d: %s", from_block, to_block, e)
        # Cursor stays at last_block; next cycle retries.
        return {"events": 0, "posts": 0, "from_block": from_block, "to_block": to_block, "error": "rpc_fetch"}

    all_events.sort(key=lambda e: (e["block"], e["tx_index"], e["log_index"]))

    # ── Phase 2: build effects map from events (no DB I/O) ──
    affected_posts = {}   # post_id -> {"users": set[str]}
    new_links = []        # list of (lpid, fpid, tpid, ic)

    for e in all_events:
        name = e["event_name"]
        evt = e["evt"]
        if name == "StakeAdded":
            pid = evt.args.postId
            staker = evt.args.staker.lower()
            affected_posts.setdefault(pid, {"users": set()})["users"].add(staker)
        elif name == "StakeWithdrawn":
            pid = evt.args.postId
            staker = evt.args.staker.lower()
            affected_posts.setdefault(pid, {"users": set()})["users"].add(staker)
        elif name == "PostUpdated":
            affected_posts.setdefault(evt.args.postId, {"users": set()})
        elif name == "PostCreated":
            pid = evt.args.postId
            creator = evt.args.creator.lower()
            affected_posts.setdefault(pid, {"users": set()})["users"].add(creator)
        elif name == "EdgeAdded":
            link_pid = evt.args.linkPostId
            from_pid = evt.args["from"]
            to_pid   = evt.args["to"]
            ic       = evt.args.isChallenge
            new_links.append((link_pid, from_pid, to_pid, ic))
            for p in (from_pid, to_pid, link_pid):
                affected_posts.setdefault(p, {"users": set()})

    # ── Phase 3: apply effects atomically ──
    try:
        # Index links first (chain_link rows must exist before we expand
        # _list_connected_posts for those links).
        for (lpid, fpid, tpid, ic) in new_links:
            index_link_canonical(db, lpid, fpid, tpid, ic)

        # Connected-post expansion (one hop). Run AFTER links written
        # so newly-added edges contribute to the connectivity.
        for (lpid, fpid, tpid, _ic) in new_links:
            for cpid in _list_connected_posts(db, fpid) | _list_connected_posts(db, tpid):
                affected_posts.setdefault(cpid, {"users": set()})

        # Index each affected post's canonical state. Enqueue derived
        # state for claim posts.
        for pid, meta in affected_posts.items():
            users = list(meta["users"]) or None
            result = index_post_canonical(db, pid, user_addresses=users)
            if result["content_type"] == 0:
                enqueue_derived_state(db, pid, is_new=result["is_new"])

        # patch_bundle04_5_chain_tx_phase3_writes
        # Per-event chain-sourced transaction history.
        # Runs inside the same transaction as the cursor
        # advance, so rollback discards these rows on failure.
        try:
            _ct_internal = _chain_tx_internal_addresses()
            _ct_fee_summary = _compute_fee_summary_by_tx(all_events, _ct_internal)
            for _ev in all_events:
                _record_chain_tx_for_event(db, w3, _ev, _ct_internal, _ct_fee_summary)
        except Exception as _ct_e:
            # chain_tx is forensic; a failure here should NOT
            # block the canonical write. Log and continue;
            # the outer txn still commits.
            logger.warning("chain_tx write failed (canonical unaffected): %s", _ct_e)

        # Global stats
        try:
            index_global_stats_canonical(db)
        except Exception as e:
            # Treat as best-effort within the txn. Roll back the
            # global-stats writes only if they're isolated, but the
            # poll is atomic — so we let it raise out and roll back
            # the whole range. Safer.
            raise

        _set_last_block(db, to_block)
        db.commit()

        if all_events:
            logger.info(
                "Atomic poll: range %d-%d, %d events, %d posts, %d links",
                from_block, to_block, len(all_events), len(affected_posts), len(new_links))

        # patch_bundle04_followup: incremental article-cache update for
        # stake/VS changes on affected posts. Best-effort, post-commit:
        # failures don't roll back the canonical state. Matches the
        # behavior of the pre-bundle-4 chain_indexer that I'd dropped.
        if affected_posts:
            _apply_stake_deltas_best_effort(db, list(affected_posts.keys()))

        return {
            "events": len(all_events),
            "posts":  len(affected_posts),
            "links":  len(new_links),
            "from_block": from_block,
            "to_block":   to_block,
        }
    except Exception as e:
        logger.warning("Atomic poll DB write failed for range %d-%d: %s",
                       from_block, to_block, e)
        db.rollback()
        return {"events": 0, "posts": 0, "from_block": from_block, "to_block": to_block, "error": "db_write"}


# ── Full sync (startup) ───────────────────────────────────────────

def _derived_state_complete(db: Session, post_id: int) -> bool:
    """patch_dedup_fix: True if a completed derived_state_queue row already
    exists for this post (in either queue_kind). The queue itself is the
    source of truth about prior derivation work — using claim.topic as
    the signal (previous approach) was wrong because topic legitimately
    remains NULL when detect_topic() returns no confident topic for
    short or generic claims, causing those posts to be re-derived on
    every worker restart.

    Used by full_sync to skip re-enqueueing derivation work for posts
    that have already been processed at least once. The derived pipeline
    is idempotent so re-enqueueing is correctness-safe; this check is
    purely an LLM-cost optimization."""
    row = db.execute(sql_text(
        "SELECT 1 FROM derived_state_queue "
        "WHERE post_id = :p AND status = 'completed' "
        "LIMIT 1"
    ), {"p": post_id}).fetchone()
    return row is not None


def full_sync(db: Session):
    """Sync all posts and links from chain to DB at startup.

    Writes canonical state synchronously for every post (fast); enqueues
    derived-state work for the worker to chew through asynchronously.
    Startup is bounded by chain reads + DB writes, not by external API
    latency. Result: ~seconds instead of ~minutes.
    """
    w3 = _get_w3()
    reg = w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=_load_abi("PostRegistry"))
    lg = w3.eth.contract(
        address=Web3.to_checksum_address(LINK_GRAPH_ADDRESS),
        abi=_load_abi("LinkGraph"))

    try:
        next_post_id = reg.functions.nextPostId().call()
    except Exception as e:
        logger.error("Full sync: cannot read nextPostId: %s", e)
        return

    last_post = next_post_id - 1
    logger.info("Full sync: indexing posts 1..%d (canonical only)", last_post)

    # Index posts
    for pid in range(1, next_post_id):
        try:
            result = index_post_canonical(db, pid)
            # patch_bundle04_followup: skip enqueue if derived state is
            # already complete (claim.topic IS NOT NULL). Avoids re-doing
            # LLM-bound work for every claim on each worker restart.
            if result["content_type"] == 0 and not _derived_state_complete(db, pid):
                enqueue_derived_state(db, pid, is_new=result["is_new"])
            db.commit()
        except Exception as e:
            logger.warning("Full sync: post %d failed: %s", pid, e)
            db.rollback()

    # Index links
    for pid in range(1, next_post_id):
        try:
            outgoing = lg.functions.getOutgoing(pid).call()
            for edge in outgoing:
                to_id, link_pid, is_challenge = edge[0], edge[1], edge[2]
                index_link_canonical(db, link_pid, pid, to_id, is_challenge)
            db.commit()
        except Exception as e:
            logger.debug("Full sync: link iter for post %d: %s", pid, e)
            db.rollback()

    # Global stats
    try:
        index_global_stats_canonical(db)
        db.commit()
    except Exception as e:
        logger.warning("Full sync: global stats failed: %s", e)
        db.rollback()

    logger.info("Full sync complete: %d posts indexed (derived-state queued)", last_post)


# ── Compatibility wrapper: old call sites that want all-in-one ────
# Kept for full_sync's per-post path inside the (legacy) startup flow
# and for any external callers (e.g., a future manual backfill CLI).
# New code in the poll loop calls index_post_canonical + enqueue_derived_state
# directly.

def index_post(db: Session, post_id: int, user_addresses: list[str] | None = None):
    """Index one post (canonical + derived-state queued). Commits."""
    try:
        result = index_post_canonical(db, post_id, user_addresses=user_addresses)
        if result["content_type"] == 0:
            enqueue_derived_state(db, post_id, is_new=result["is_new"])
        db.commit()
    except Exception as e:
        logger.warning("index_post(%d) failed: %s", post_id, e)
        db.rollback()


def index_link(db: Session, link_post_id: int, from_post_id: int,
               to_post_id: int, is_challenge: bool):
    """Compatibility wrapper for index_link_canonical that commits."""
    try:
        index_link_canonical(db, link_post_id, from_post_id, to_post_id, is_challenge)
        db.commit()
    except Exception as e:
        logger.warning("index_link(%d) failed: %s", link_post_id, e)
        db.rollback()


def _reindex_connected(db: Session, post_id: int):
    """Re-index posts connected to this one via links. Compatibility wrapper.
    Each connected post gets its own commit via index_post."""
    for cpid in _list_connected_posts(db, post_id):
        index_post(db, cpid)


# ── Background thread ─────────────────────────────────────────────

_indexer_thread = None

def start_indexer():
    """Start the background indexer thread."""
    global _indexer_thread
    if _indexer_thread is not None and _indexer_thread.is_alive():
        return

    def _run():
        logger.info("Chain indexer starting (atomic, single-cursor, depth=%d)...",
                    CONFIRMATION_DEPTH)

        # Initial full sync — canonical only; derived state queued.
        try:
            db = get_session_factory()()
            try:
                full_sync(db)
            finally:
                db.close()
        except Exception as e:
            logger.error("Full sync failed: %s", e)

        # Poll loop
        while True:
            try:
                db = get_session_factory()()
                try:
                    poll_events_atomic(db)
                    resolve_pending_txs(db)
                finally:
                    db.close()
            except Exception as e:
                logger.warning("Indexer poll outer error: %s", e)
            time.sleep(POLL_INTERVAL)

    _indexer_thread = threading.Thread(target=_run, daemon=True, name="chain-indexer")
    _indexer_thread.start()
    logger.info("Chain indexer thread started (poll every %ds)", POLL_INTERVAL)


# ── tx_log confirmation watcher (Bundle 4a) ───────────────────────
# Resolves pending tx_log rows by fetching their receipts. Called from
# the poll loop after poll_events_atomic. Pure status-flip; does NOT
# write chain_* tables — the indexer's normal event stream does that.

TX_PENDING_TIMEOUT_SECONDS = 600   # 10 min
TX_WATCHER_BATCH_LIMIT     = 50
TX_WATCHER_MIN_AGE_SECONDS = 5


def _extract_post_id_from_receipt(receipt, action_type, calldata_hex):
    """Best-effort post_id extraction from a confirmed receipt's logs/calldata."""
    try:
        if action_type == "stake":
            cd = calldata_hex
            if cd.startswith("0x"):
                cd = cd[2:]
            if len(cd) >= 8 + 64:
                return int(cd[8:8+64], 16)
            return None

        w3 = _get_w3()
        if action_type == "claim":
            reg = w3.eth.contract(
                address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
                abi=_load_abi("PostRegistry"))
            from web3.logs import DISCARD
            logs = reg.events.PostCreated().process_receipt(receipt, errors=DISCARD)
            if logs:
                return int(logs[0].args.postId)
            return None

        if action_type == "link":
            lg = w3.eth.contract(
                address=Web3.to_checksum_address(LINK_GRAPH_ADDRESS),
                abi=_load_abi("LinkGraph"))
            from web3.logs import DISCARD
            logs = lg.events.EdgeAdded().process_receipt(receipt, errors=DISCARD)
            if logs:
                return int(logs[0].args.linkPostId)
            return None
    except Exception as e:
        logger.debug("post_id extraction failed (action=%s): %s", action_type, e)
    return None


def _decode_revert_message(receipt):
    return f"Transaction reverted (gasUsed={getattr(receipt, 'gasUsed', '?')})"


INDEXER_LAG_WARN_BLOCKS = 10  # patch_bundle04_p2: warn if indexer is this many blocks behind a confirmed tx


def _check_indexer_lag(db, tx_block: int, tx_log_id: int) -> None:
    """Log a warning if the event indexer's cursor is sustained-behind
    the block where a tx was just confirmed. Brief lag (<10 blocks) is
    normal — the watcher fetches by hash so it can outrun the event
    poller by a few blocks."""
    try:
        row = db.execute(sql_text(
            "SELECT value FROM chain_indexer_state WHERE key = :k"
        ), {"k": CURSOR_KEY}).fetchone()
        if not row:
            return
        last_block = int(row[0])
        lag = tx_block - last_block
        if lag > INDEXER_LAG_WARN_BLOCKS:
            logger.warning(
                "indexer-lag: tx_log %d confirmed at block %d but indexer cursor at %d (%d blocks behind)",
                tx_log_id, tx_block, last_block, lag)
    except Exception as e:
        logger.debug("_check_indexer_lag failed: %s", e)


def _log_relay_gas(db, row, receipt):
    """patch_wire_relay_fee_log: persist the relay's AVAX gas burn for one resolved
    tx into relay_fee_log. Best-effort: never raises into the resolver. gas cost is
    authoritative here; fee_charged_vsp is filled later by a JOIN on chain_tx.fee_vsp
    (written on a different pass), so it is left NULL at write time."""
    try:
        gas_used = int(receipt.gasUsed)
        # patch_fix_relay_gas_price_zero: read effectiveGasPrice robustly. On this web3
        # version the receipt is an AttributeDict whose key is reached via [] not getattr,
        # so getattr returned None and the old code defaulted to 0 -> zero cost. Try dict
        # access, then attr, then FALL BACK to the tx's gas price. Never default to 0.
        egp = None
        try:
            egp = receipt["effectiveGasPrice"]
        except (KeyError, TypeError):
            egp = getattr(receipt, "effectiveGasPrice", None)
        if egp is None:
            try:
                from tx_signer import build_w3
                _w3 = build_w3()
                tx = _w3.eth.get_transaction(receipt["transactionHash"] if "transactionHash" in receipt
                                             else getattr(receipt, "transactionHash", row.tx_hash))
                egp = tx.get("gasPrice") if hasattr(tx, "get") else getattr(tx, "gasPrice", None)
            except Exception:
                egp = None
        egp = int(egp) if egp is not None else 0
        if egp == 0:
            logger.warning("relay_fee_log: effectiveGasPrice unresolved for %s; gas cost will be 0",
                           getattr(row, "tx_hash", "?"))
        gas_price_gwei = float(egp) / 1e9
        gas_cost_avax = (gas_used * float(egp)) / 1e18
        try:
            from mm.oracle import get_avax_price_usd as _avax
            avax_usd = _avax() or 0.0
        except Exception:
            from config import AVAX_PRICE_USD as avax_usd
        gas_cost_usd = gas_cost_avax * float(avax_usd)
        db.execute(sql_text(
            "INSERT INTO relay_fee_log "
            "(tx_hash, user_address, gas_used, gas_price_gwei, gas_cost_avax, "
            " gas_cost_usd, tx_type) "
            "VALUES (:h, :u, :gu, :gp, :ca, :cu, :tt)"
        ), {
            "h": row.tx_hash,
            "u": (row.user_address if getattr(row, "user_address", None) else "unknown"),
            "gu": gas_used,
            "gp": round(gas_price_gwei, 4),
            "ca": gas_cost_avax,
            "cu": gas_cost_usd,
            "tt": getattr(row, "action_type", None),
        })
        # caller commits as part of its existing db.commit()
    except Exception as e:
        logger.warning("relay_fee_log write skipped for %s: %s",
                       getattr(row, "tx_hash", "?"), e)


def resolve_pending_txs(db):
    """Resolve pending tx_log rows by fetching their receipts. Called from the
    indexer poll loop after poll_events_atomic. No-op if nothing's pending."""
    import tx_log as _tx_log
    try:
        rows = _tx_log.get_pending_for_watcher(
            db,
            min_age_seconds=TX_WATCHER_MIN_AGE_SECONDS,
            limit=TX_WATCHER_BATCH_LIMIT,
        )
    except Exception as e:
        logger.warning("resolve_pending_txs: query for pending rows failed: %s", e)
        return

    if not rows:
        return

    w3 = _get_w3()
    for row in rows:
        try:
            try:
                receipt = w3.eth.get_transaction_receipt(row.tx_hash)
            except Exception:
                if row.age_sec > TX_PENDING_TIMEOUT_SECONDS:
                    _tx_log.mark_dropped(db, row.id)
                    db.commit()
                    logger.info("tx_log %d: dropped (timeout) %s", row.id, row.tx_hash)
                continue

            if receipt is None:
                if row.age_sec > TX_PENDING_TIMEOUT_SECONDS:
                    _tx_log.mark_dropped(db, row.id)
                    db.commit()
                continue

            if int(receipt.status) == 1:
                post_id = _extract_post_id_from_receipt(receipt, row.action_type, row.calldata)
                _tx_log.mark_confirmed(
                    db, row.id,
                    block_number=int(receipt.blockNumber),
                    gas_used=int(receipt.gasUsed),
                    post_id=post_id,
                )
                db.commit()
                logger.info("tx_log %d: confirmed %s block=%d post_id=%s",
                            row.id, row.tx_hash, receipt.blockNumber, post_id)
                _log_relay_gas(db, row, receipt); db.commit()  # patch_wire_relay_fee_log
                # patch_bundle04_p2: indexer-lag alert. The watcher resolves
                # by receipt-hash and can be ahead of the event indexer
                # briefly; only warn on sustained lag.
                _check_indexer_lag(db, int(receipt.blockNumber), row.id)
            else:
                err = _decode_revert_message(receipt)
                _tx_log.mark_reverted(
                    db, row.id,
                    error_message=err,
                    block_number=int(receipt.blockNumber),
                    gas_used=int(receipt.gasUsed),
                )
                db.commit()
                logger.info("tx_log %d: reverted %s block=%d",
                            row.id, row.tx_hash, receipt.blockNumber)
                _log_relay_gas(db, row, receipt); db.commit()  # patch_wire_relay_fee_log
        except Exception as e:
            logger.warning("resolve_pending_txs: failed to resolve row %d: %s", row.id, e)
            try: db.rollback()
            except Exception: pass
            continue


# ── Manual backfill CLI ─────────────────────────────────────────
# Invoke from inside the worker or app container:
#   python -m chain_indexer backfill --post-id N
#   python -m chain_indexer backfill --from-block X --to-block Y
#
# Out-of-band re-processing. Bypasses the cursor (does NOT update
# last_block_global). Uses the same canonical-write path as live polling.

def _backfill_post(post_id: int) -> None:
    """Re-index one post by id. Useful for fixing drift identified by audit."""
    db = get_session_factory()()
    try:
        result = index_post_canonical(db, post_id)
        if result["content_type"] == 0:
            enqueue_derived_state(db, post_id, is_new=result["is_new"])
        db.commit()
        print(f"backfill: post {post_id} re-indexed "
              f"(content_type={result['content_type']}, "
              f"is_new={result['is_new']})")
    except Exception as e:
        db.rollback()
        print(f"backfill: post {post_id} failed: {e}")
    finally:
        db.close()


def _backfill_block_range(from_block: int, to_block: int) -> None:
    """Re-process events in a block range. Does NOT update last_block_global —
    purely out-of-band re-application. Affected posts will get their
    canonical state re-written and derived state re-enqueued."""
    w3 = _get_w3()
    contracts = _get_contracts(w3)
    all_events = []
    for contract_name, (contract, event_names) in contracts.items():
        for event_name in event_names:
            event_obj = getattr(contract.events, event_name)
            for evt in event_obj.get_logs(from_block=from_block, to_block=to_block):
                all_events.append({
                    "block": evt.blockNumber, "tx_index": evt.transactionIndex,
                    "log_index": evt.logIndex, "contract": contract_name,
                    "event_name": event_name, "evt": evt,
                })
    all_events.sort(key=lambda e: (e["block"], e["tx_index"], e["log_index"]))

    affected_posts = {}
    new_links = []
    for e in all_events:
        name, evt = e["event_name"], e["evt"]
        if name in ("StakeAdded", "StakeWithdrawn"):
            pid = evt.args.postId
            staker = evt.args.staker.lower()
            affected_posts.setdefault(pid, {"users": set()})["users"].add(staker)
        elif name == "PostUpdated":
            affected_posts.setdefault(evt.args.postId, {"users": set()})
        elif name == "PostCreated":
            pid = evt.args.postId
            creator = evt.args.creator.lower()
            affected_posts.setdefault(pid, {"users": set()})["users"].add(creator)
        elif name == "EdgeAdded":
            link_pid = evt.args.linkPostId
            from_pid = evt.args["from"]
            to_pid   = evt.args["to"]
            ic       = evt.args.isChallenge
            new_links.append((link_pid, from_pid, to_pid, ic))
            for p in (from_pid, to_pid, link_pid):
                affected_posts.setdefault(p, {"users": set()})

    db = get_session_factory()()
    try:
        for (lpid, fpid, tpid, ic) in new_links:
            index_link_canonical(db, lpid, fpid, tpid, ic)
        for (lpid, fpid, tpid, _ic) in new_links:
            for cpid in _list_connected_posts(db, fpid) | _list_connected_posts(db, tpid):
                affected_posts.setdefault(cpid, {"users": set()})
        for pid, meta in affected_posts.items():
            users = list(meta["users"]) or None
            result = index_post_canonical(db, pid, user_addresses=users)
            if result["content_type"] == 0:
                enqueue_derived_state(db, pid, is_new=result["is_new"])
        db.commit()
        print(f"backfill: range {from_block}-{to_block}, "
              f"{len(all_events)} events, {len(affected_posts)} posts, "
              f"{len(new_links)} links re-applied")
    except Exception as e:
        db.rollback()
        print(f"backfill: range {from_block}-{to_block} failed: {e}")
    finally:
        db.close()


def _cli_main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="chain_indexer manual backfill")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="re-index specific posts or block ranges")
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--post-id", type=int, help="re-index a single post by id")
    g.add_argument("--from-block", type=int, help="re-process events in [from-block, to-block]")
    b.add_argument("--to-block", type=int, help="upper bound for --from-block (required with --from-block)")
    args = p.parse_args()

    # Configure logging so backfill output is visible.
    import logging, sys as _sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=_sys.stdout)

    if args.cmd == "backfill":
        if args.post_id is not None:
            _backfill_post(args.post_id)
            return 0
        if args.from_block is not None:
            if args.to_block is None:
                p.error("--to-block is required with --from-block")
            if args.to_block < args.from_block:
                p.error("--to-block must be >= --from-block")
            _backfill_block_range(args.from_block, args.to_block)
            return 0
    return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli_main())
