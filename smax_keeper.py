#!/usr/bin/env python3
"""app/smax_keeper.py — patch_smax_keeper

S-03 layer (ii), operational half. Core's PR-C (#14) made StakeEngine.refreshSMax
permissionless: anyone can settle a post and feed its TRUE stored total into the
sMax leader tracker. This module is the party that volunteers: once per cycle it
checks whether the on-chain sMax has deviated from reality and pokes when — and
only when — a transaction would actually change something.

WHY THIS EXISTS (architecture note, recorded deliberately):
  Accrual is lazy, so a dormant post's true total does not exist on-chain until
  someone settles it. No tracker, however wide, can see a number that has never
  been materialized — the deviation is a property of lazy accrual, not of the
  tracker. Core therefore guarantees I.4 for TRACKED posts and makes the residue
  CLOSEABLE BY ANYONE (the permissionless poke). This keeper makes "anyone"
  scheduled instead of hoped-for. It is NOT privileged infrastructure: if this
  worker dies, any user interaction or any third party's refreshSMax call closes
  the same gap, and the failure mode is fairness (one dormant post earning the
  rMax ceiling instead of a participation-scaled rate — a rate the protocol
  already permits), never solvency. Mainnet graduation path: register the same
  poke with Chainlink Automation / Gelato so liveness doesn't depend on one
  container; this worker then becomes the backup.

COST MODEL (measured against core's test gas numbers):
  A poke that settles a dormant post runs ~150k–500k gas depending on ranked-
  queue depth; a poke on an already-settled post is ~50–80k (tracker feed only).
  The decision logic below sends AT MOST a handful of transactions per epoch and
  usually zero: deviations are closed when seen, and decay is materialized at
  most once per epoch. On Avalanche gas prices this is single-digit dollars per
  month at the high end. The keeper wallet holds ONLY gas AVAX — no protocol
  funds, no privileges (relay-key-separation posture, see relay_wallet.py).

DECISION RULES per cycle (pure function `_decide`, unit-tested without a chain):
  1. DEVIATION: some candidate post's (projected) total exceeds on-chain sMax
     -> poke the largest such post. Re-check after each poke; cap per cycle.
     Not epoch-gated: a visible I.4 deviation is closed whenever seen.
  2. DECAY MATERIALIZATION: no deviation, but sMax sits above every candidate
     total and its bookkeeping epoch is stale -> poke the largest candidate,
     at most ONCE per epoch (cursor in chain_indexer_state), so decay ticks
     down smoothly instead of lumping at the 30-epoch catch-up cap.
  3. Otherwise: no transaction. Most cycles land here.

CANDIDATES come from the indexer's own chain_post table (top N by indexed
totals) — the DB is only a shortlist filter; every decision input is re-read
from the chain, so indexer staleness can delay a poke but never cause a wrong
one. refreshSMax itself can only feed a post's true stored total, so the worst
this keeper can ever do on-chain is make sMax more honest.

CONFIGURATION (all env; fail-IDLE, not fail-loud — this is an optional
maintenance job, unlike the relay which sits in the user tx path):
  KEEPER_KMS_KEY / KEEPER_ADDRESS   GCP KMS key resource + expected EOA, same
                                    ceremony as RELAY/MM (signing/kms_account).
                                    Both unset -> keeper logs once and idles.
  KEEPER_INTERVAL_SEC        cycle interval          (default 900)
  KEEPER_CANDIDATES          shortlist size          (default 10 = TRACKED_POSTS)
  KEEPER_MAX_POKES_PER_CYCLE deviation-poke cap      (default 3)
  KEEPER_EPOCH_SEC           epoch length in seconds (default 86400, must match
                             core EPOCH_LENGTH; used for epoch arithmetic only)
  KEEPER_MIN_BALANCE_WEI     low-gas alert threshold (default 5e17 = 0.5 AVAX)

Heavy imports (web3, KMS, db) are deliberately lazy so importing this module —
and running its pure-logic tests — needs nothing but the standard library.
"""
import logging
import os

logger = logging.getLogger(__name__)

# ── Tunables (env-overridable; defaults need no env-file edit to ship) ──
KEEPER_INTERVAL_SEC = int(os.getenv("KEEPER_INTERVAL_SEC", "900"))
KEEPER_CANDIDATES = int(os.getenv("KEEPER_CANDIDATES", "10"))
KEEPER_MAX_POKES_PER_CYCLE = int(os.getenv("KEEPER_MAX_POKES_PER_CYCLE", "3"))
KEEPER_EPOCH_SEC = int(os.getenv("KEEPER_EPOCH_SEC", "86400"))
KEEPER_MIN_BALANCE_WEI = int(os.getenv("KEEPER_MIN_BALANCE_WEI", str(5 * 10**17)))

CURSOR_KEY = "smax_keeper_last_decay_poke_epoch"

# Lazily-built singletons (chain handle, contract, signer). Built on first
# configured poll; a build failure is logged and retried next cycle rather
# than taking the worker process down.
_state = {"built": False, "w3": None, "engine": None, "account": None,
          "sign_and_send": None}


def is_configured() -> bool:
    """True when a keeper key is provisioned. Safe to call unconfigured —
    reads env only, imports nothing heavy."""
    return bool(os.getenv("KEEPER_KMS_KEY", "").strip()
                and os.getenv("KEEPER_ADDRESS", "").strip())


def keeper_address() -> str:
    return os.getenv("KEEPER_ADDRESS", "").strip()


# ── Pure decision logic (no chain, no db — unit-tested directly) ──────────
def _decide(smax: int, smax_last_epoch: int, current_epoch: int,
            candidates, decay_poked_epoch: int):
    """Given the on-chain picture, return (action, post_id, reason).

    candidates: list of (post_id, true_total) re-read from chain, any order.
    decay_poked_epoch: last epoch a decay-materialization poke was sent.

    action is one of: 'deviation' | 'decay' | None.
    """
    if not candidates:
        return (None, None, "no candidate posts")
    top_pid, top_total = max(candidates, key=lambda c: c[1])
    if top_total > smax:
        return ("deviation", top_pid,
                f"post {top_pid} total {top_total} > sMax {smax}")
    if (smax > top_total
            and smax_last_epoch < current_epoch
            and decay_poked_epoch < current_epoch):
        return ("decay", top_pid,
                f"sMax {smax} above leader {top_total}, bookkeeping at epoch "
                f"{smax_last_epoch} < {current_epoch}: materialize decay")
    return (None, None, "sMax honest; nothing to do")


# ── Chain / db plumbing (lazy) ────────────────────────────────────────────
def _build():
    """Build w3 + contract + KMS signer once. Raises on failure (caller logs
    and retries next cycle)."""
    if _state["built"]:
        return
    from web3 import Web3
    from chain.provider import w3            # shared app-wide write handle
    from chain.abi import load_abi_optional
    from config import STAKE_ENGINE_ADDRESS
    from signing.kms_account import kms_account_from_env
    from tx_signer import make_sign_and_send

    if not STAKE_ENGINE_ADDRESS:
        raise RuntimeError("smax_keeper: no StakeEngine address in deployments")
    abi = load_abi_optional("StakeEngine") or []
    if not any(e.get("name") == "refreshSMax" for e in abi if isinstance(e, dict)):
        raise RuntimeError(
            "smax_keeper: StakeEngine ABI has no refreshSMax — core/out is "
            "pre-PR-C; run `forge build` in core (and redeploy) first")
    engine = w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS), abi=abi)
    account = kms_account_from_env("KEEPER")  # asserts KEEPER_KMS_KEY -> KEEPER_ADDRESS
    # treasury-wallet gas policy (fallback=None): if a poke would revert,
    # surface it — never guess a gas limit and broadcast a doomed tx.
    sign_and_send = make_sign_and_send(
        account=account, w3=w3, receipt_timeout=120, gas_estimate_fallback=None,
        logger=logger, label="smax_keeper")
    _state.update(built=True, w3=w3, engine=engine, account=account,
                  sign_and_send=sign_and_send)
    logger.info("smax_keeper: initialized (engine=%s keeper=%s)",
                STAKE_ENGINE_ADDRESS, account.address)


def _db_candidates(limit: int):
    """Top-N post ids by indexed totals — shortlist only; totals re-read on-chain."""
    from db import get_session_factory
    from sqlalchemy import text as sql_text
    sess = get_session_factory()()
    try:
        rows = sess.execute(sql_text(
            "SELECT post_id FROM chain_post "
            "ORDER BY (support_total + challenge_total) DESC LIMIT :n"
        ), {"n": limit}).fetchall()
        return [int(r[0]) for r in rows]
    finally:
        sess.close()


def _get_cursor_epoch() -> int:
    from db import get_session_factory
    from sqlalchemy import text as sql_text
    sess = get_session_factory()()
    try:
        row = sess.execute(sql_text(
            "SELECT value FROM chain_indexer_state WHERE key = :k"
        ), {"k": CURSOR_KEY}).fetchone()
        return int(row[0]) if row else -1
    finally:
        sess.close()


def _set_cursor_epoch(epoch: int):
    from db import get_session_factory
    from sqlalchemy import text as sql_text
    sess = get_session_factory()()
    try:
        sess.execute(sql_text(
            "INSERT INTO chain_indexer_state (key, value, updated_at) "
            "VALUES (:k, :v, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()"
        ), {"k": CURSOR_KEY, "v": str(epoch)})
        sess.commit()
    finally:
        sess.close()


def _record_check(epoch: int, smax, pokes: int, reason: str) -> None:
    """patch_smax_keeper_check: one ops_metrics row per completed cycle, pokes or not —
    metric 'smax_keeper_check', value = pokes sent this cycle, labels {epoch, smax_vsp, reason}.
    The dashboard's "sMax keeper" panel reads its age (silent keeper = red); the decay-poke
    cursor it used to read only moves when a poke is needed, so it read red for days of
    honest "nothing to do". Never raises (instrumentation must not break the keeper)."""
    try:
        from balance_sampler import record_metric
        from db import get_session_factory
        db = get_session_factory()()
        try:
            record_metric(db, "smax_keeper_check", pokes, {
                "epoch": int(epoch),
                "smax_vsp": (None if smax is None else round(int(smax) / 1e18, 6)),
                "reason": (reason or "")[:120]})
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("smax_keeper: check metric not recorded: %s", e)


def _check_gas_balance():
    """WARN + alert when the keeper EOA runs low on gas. Never raises."""
    try:
        bal = _state["w3"].eth.get_balance(_state["account"].address)
        if bal < KEEPER_MIN_BALANCE_WEI:
            logger.warning(
                "smax_keeper: LOW GAS balance=%.4f AVAX (threshold %.4f) addr=%s",
                bal / 1e18, KEEPER_MIN_BALANCE_WEI / 1e18,
                _state["account"].address)
            try:
                import notify
                notify.send_alert(
                    "keeper_low_balance",
                    f"sMax keeper gas balance {bal / 1e18:.4f} AVAX below threshold",
                    address=_state["account"].address, balance_wei=str(bal))
            except Exception:
                pass
    except Exception as e:
        logger.warning("smax_keeper: balance check failed: %s", e)


def _poke(post_id: int, reason: str) -> bool:
    """Send refreshSMax(post_id). Returns True on success. Alerts on failure."""
    from tx_signer import TxRevertedError
    engine, account = _state["engine"], _state["account"]
    logger.info("smax_keeper: poking refreshSMax(%d) — %s", post_id, reason)
    try:
        tx = engine.functions.refreshSMax(post_id).build_transaction(
            {"from": account.address})
        tx_hash = _state["sign_and_send"](tx)
        logger.info("smax_keeper: poke landed post=%d tx=%s", post_id, tx_hash)
        return True
    except TxRevertedError as e:
        logger.error("smax_keeper: poke REVERTED post=%d tx=%s", post_id, e.tx_hash)
        _alert_poke_failed(post_id, f"reverted (tx={e.tx_hash})")
        return False
    except Exception as e:
        logger.error("smax_keeper: poke failed post=%d: %s", post_id, e)
        _alert_poke_failed(post_id, str(e))
        return False


def _alert_poke_failed(post_id: int, detail: str):
    try:
        import notify
        notify.send_alert("keeper_poke_failed",
                          f"sMax keeper poke failed for post {post_id}",
                          post_id=post_id, detail=detail[:300])
    except Exception:
        pass


def poll_once():
    """One keeper cycle. Cheap no-op when unconfigured; view calls only unless
    a transaction would actually change on-chain state."""
    if not is_configured():
        return
    _build()
    w3, engine = _state["w3"], _state["engine"]

    _check_gas_balance()

    block_ts = w3.eth.get_block("latest").timestamp
    current_epoch = block_ts // KEEPER_EPOCH_SEC

    pids = _db_candidates(KEEPER_CANDIDATES)
    if not pids:
        logger.info("smax_keeper: no posts indexed yet; nothing to do")
        _record_check(current_epoch, None, 0, "no posts indexed yet")  # patch_smax_keeper_check
        return

    # Every decision input re-read from chain (DB was only the shortlist).
    candidates = []
    for pid in pids:
        try:
            s, c = engine.functions.getPostTotals(pid).call()
            candidates.append((pid, int(s) + int(c)))
        except Exception as e:
            logger.warning("smax_keeper: getPostTotals(%d) failed: %s", pid, e)

    decay_poked_epoch = _get_cursor_epoch()
    pokes = 0
    while pokes < KEEPER_MAX_POKES_PER_CYCLE:
        smax = int(engine.functions.sMax().call())
        smax_last = int(engine.functions.sMaxLastUpdatedEpoch().call())
        action, pid, reason = _decide(
            smax, smax_last, current_epoch, candidates, decay_poked_epoch)
        if action is None:
            if pokes == 0:
                logger.info("smax_keeper: %s (sMax=%d, epoch=%d)",
                            reason, smax, current_epoch)
            _record_check(current_epoch, smax, pokes, reason)  # patch_smax_keeper_check
            return
        ok = _poke(pid, reason)
        pokes += 1
        if not ok:
            _record_check(current_epoch, smax, pokes, f"poke failed: post {pid}")  # patch_smax_keeper_check
            return  # don't hammer a failing path; alert already sent
        if action == "decay":
            _set_cursor_epoch(current_epoch)
            decay_poked_epoch = current_epoch
        # refresh this candidate's total after its settlement, then loop to
        # re-read sMax and re-decide (a second dormant giant may now deviate).
        try:
            s, c = engine.functions.getPostTotals(pid).call()
            candidates = [(p, t) if p != pid else (pid, int(s) + int(c))
                          for (p, t) in candidates]
        except Exception:
            pass
    logger.warning("smax_keeper: poke cap (%d) reached this cycle; will re-check "
                   "next cycle", KEEPER_MAX_POKES_PER_CYCLE)
    _record_check(current_epoch, smax, pokes, "poke cap reached; re-check next cycle")  # patch_smax_keeper_check
