"""Indexer self-audit task.

Periodically samples recently-indexed posts, re-reads their state from
the chain, and compares to what's in chain_post. Any drift is recorded
in indexer_audit_log for manual review.

This catches:
  - Indexed state diverged from chain (e.g., due to a missed event,
    reorg, or upstream bug)
  - Posts present in DB that no longer exist on chain (shouldn't happen
    unless DB was restored from an old backup)
  - Posts in chain_post but no longer indexable (RPC failures during
    audit aren't drift — they're noisy and we skip)

Findings are advisory; no automatic remediation. Operators review the
audit log periodically and use the manual backfill CLI (also in patch 2)
to re-index drifted posts.

Started as its own thread from worker.py.
"""

import logging
import random
import threading
import time
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_session_factory

logger = logging.getLogger(__name__)

# ── Tuning ─────────────────────────────────────────────────────────
# Audit cycle: every N seconds, sample M posts, compare each.
AUDIT_INTERVAL_SECONDS = 300        # 5 minutes
AUDIT_SAMPLE_SIZE      = 20         # posts per cycle
AUDIT_RECENT_WINDOW    = 86400      # posts indexed in last N seconds are candidates
# Tolerance for floating-point comparisons (chain values come from
# 1e18-scaled integers; rounding can give tiny diffs).
FLOAT_EPSILON          = 1e-6

_audit_thread = None


# ── Field comparators ──────────────────────────────────────────────

def _floats_match(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= FLOAT_EPSILON
    except (TypeError, ValueError):
        return False


def _bools_match(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return bool(a) == bool(b)


# ── Per-post audit ────────────────────────────────────────────────

def _audit_one_post(db: Session, post_id: int) -> int:
    """Audit one post. Returns number of drift findings written."""
    # chain_indexer imports done inside the function to avoid circular
    # imports (chain_indexer imports nothing from indexer_audit).
    from chain_indexer import _get_w3, _load_abi
    from web3 import Web3
    from config import (
        STAKE_ENGINE_ADDRESS,
        SCORE_ENGINE_ADDRESS,
        POST_REGISTRY_ADDRESS,
    )

    # Read DB state
    db_row = db.execute(sql_text("""
        SELECT support_total, challenge_total, base_vs, effective_vs, is_active
        FROM chain_post
        WHERE post_id = :p
    """), {"p": post_id}).fetchone()

    if not db_row:
        # Post in audit sample but not in chain_post — that's odd, but
        # not technically drift (the sampler shouldn't produce this).
        logger.debug("audit: post %d not in chain_post, skipping", post_id)
        return 0

    # Read chain state
    try:
        w3 = _get_w3()
        se = w3.eth.contract(
            address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
            abi=_load_abi("StakeEngine"))
        sc = w3.eth.contract(
            address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS),
            abi=_load_abi("ScoreEngine"))

        support_wei, challenge_wei = se.functions.getPostTotals(post_id).call()
        chain_support = support_wei / 1e18
        chain_challenge = challenge_wei / 1e18
        chain_total = chain_support + chain_challenge
        chain_active = chain_total >= 1.0

        # patch_vs_single_source: a failed read is not a chain value of 0.0 — it would
        # log a phantom 'drift' on every post. Skip the post instead.
        from chain.vs import read_effective_vs_pct, read_base_vs_pct
        try:
            chain_effective_vs = read_effective_vs_pct(sc, post_id)
            chain_base_vs = read_base_vs_pct(sc, post_id)
        except Exception as e:
            logger.warning("audit: post %d VS read failed, skipping: %s", post_id, e)
            return 0
    except Exception as e:
        # RPC failed — not drift; skip and try this post next cycle.
        logger.debug("audit: chain read failed for post %d: %s", post_id, e)
        return 0

    # Compare and write findings
    findings = []
    if not _floats_match(db_row.support_total, chain_support):
        findings.append(("support_total", str(db_row.support_total), str(chain_support)))
    if not _floats_match(db_row.challenge_total, chain_challenge):
        findings.append(("challenge_total", str(db_row.challenge_total), str(chain_challenge)))
    if not _floats_match(db_row.base_vs, chain_base_vs):
        findings.append(("base_vs", str(db_row.base_vs), str(chain_base_vs)))
    if not _floats_match(db_row.effective_vs, chain_effective_vs):
        findings.append(("effective_vs", str(db_row.effective_vs), str(chain_effective_vs)))
    if not _bools_match(db_row.is_active, chain_active):
        findings.append(("is_active", str(db_row.is_active), str(chain_active)))

    for (field, dbv, chv) in findings:
        db.execute(sql_text("""
            INSERT INTO indexer_audit_log (post_id, field, db_value, chain_value, drift_kind)
            VALUES (:p, :f, :dv, :cv, 'mismatch')
        """), {"p": post_id, "f": field, "dv": dbv, "cv": chv})

    if findings:
        db.commit()
        logger.warning(
            "audit: post %d drift in %d field(s): %s",
            post_id, len(findings),
            ", ".join(f"{f} db={dv} chain={cv}" for (f, dv, cv) in findings))

    return len(findings)


# ── Cycle ─────────────────────────────────────────────────────────

def _pick_audit_sample(db: Session, n: int) -> list:
    """Return post_ids for the next audit cycle. Bias towards recently-indexed."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=AUDIT_RECENT_WINDOW)).isoformat()
    rows = db.execute(sql_text("""
        SELECT post_id FROM chain_post
        WHERE indexed_at > :cutoff
        ORDER BY indexed_at DESC
        LIMIT 200
    """), {"cutoff": cutoff}).fetchall()
    if not rows:
        # Empty recent window — fall back to a random sample of all posts
        rows = db.execute(sql_text(
            "SELECT post_id FROM chain_post ORDER BY post_id DESC LIMIT 200"
        )).fetchall()
    candidate_ids = [r[0] for r in rows]
    if len(candidate_ids) <= n:
        return candidate_ids
    return random.sample(candidate_ids, n)


def run_one_audit_cycle(db: Session) -> dict:
    """Run a single audit cycle. Returns stats."""
    sample = _pick_audit_sample(db, AUDIT_SAMPLE_SIZE)
    if not sample:
        return {"audited": 0, "drift_count": 0}

    total_drift = 0
    for post_id in sample:
        try:
            total_drift += _audit_one_post(db, post_id)
        except Exception as e:
            logger.warning("audit: error auditing post %d: %s", post_id, e)
            try: db.rollback()
            except Exception: pass

    if total_drift > 0:
        logger.warning("audit cycle: %d posts audited, %d total drift findings",
                       len(sample), total_drift)
    else:
        logger.info("audit cycle: %d posts audited, no drift", len(sample))

    return {"audited": len(sample), "drift_count": total_drift}


# ── Thread management ─────────────────────────────────────────────

def _run():
    logger.info("Indexer audit task starting (interval=%ds, sample=%d)...",
                AUDIT_INTERVAL_SECONDS, AUDIT_SAMPLE_SIZE)
    # Initial delay to let full_sync settle
    time.sleep(60)
    while True:
        try:
            db = get_session_factory()()
            try:
                run_one_audit_cycle(db)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Indexer audit outer error: %s", e)
        time.sleep(AUDIT_INTERVAL_SECONDS)


def start_indexer_audit():
    global _audit_thread
    if _audit_thread is not None and _audit_thread.is_alive():
        return
    _audit_thread = threading.Thread(target=_run, daemon=True, name="indexer-audit")
    _audit_thread.start()
    logger.info("Indexer audit thread started")
