"""settle_keeper.py — Game B epoch settlement pass (whitepaper v17 §3.2, §5.4).

Settlement pays on the effective pool, computed live from a post's ancestors. A user
transaction settles its post inline when the walk fits USER_SETTLE_GAS; otherwise the
StakeEngine reverts SettleFirst(postId) and the post must be settled by the permissionless
updatePost(). This keeper settles EVERY post once per epoch, parents before children
(topological order over chain_link from -> to), so:
  * money never waits on a user transaction (G7: daily settlement = daily compounding),
  * children settle against parents that already settled this epoch,
  * SettleFirst is rare in practice (the pass runs shortly after each boundary).

Signing: the same KMS keeper account as smax_keeper (reuses its built state). Sync; the
worker runs it in a thread. Idempotent: a post already at the current epoch is skipped
(read via getLastSnapshotEpoch), so re-runs and overlapping cycles are harmless.

Env: KEEPER_EPOCH_SEC (default 86400), SETTLE_KEEPER_MARGIN_SEC (default 600) — the pass
starts this long after the boundary so the last-second stakes are in the window,
SETTLE_KEEPER_MAX_PER_CYCLE (default 200).
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

EPOCH_SEC = int(os.getenv("KEEPER_EPOCH_SEC", "86400"))
MARGIN_SEC = int(os.getenv("SETTLE_KEEPER_MARGIN_SEC", "600"))
MAX_PER_CYCLE = int(os.getenv("SETTLE_KEEPER_MAX_PER_CYCLE", "200"))

_last_pass_epoch = -1
_stats = {"passes": 0, "settled": 0, "failed": 0, "last_lag_posts": 0}


def stats() -> dict:
    return dict(_stats)


def topo_order(post_ids: list[int], edges: list[tuple[int, int]]) -> list[int]:
    """Kahn's algorithm over (from_post_id -> to_post_id). Cycles: remaining nodes appended in
    id order (the contract zeroes the closing path; order among them is immaterial)."""
    ids = set(post_ids)
    indeg = {p: 0 for p in ids}
    out = defaultdict(list)
    for a, b in edges:
        if a in ids and b in ids:
            out[a].append(b)
            indeg[b] += 1
    q = deque(sorted(p for p in ids if indeg[p] == 0))
    order = []
    while q:
        p = q.popleft()
        order.append(p)
        for c in sorted(out[p]):
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if len(order) < len(ids):
        order.extend(sorted(ids - set(order)))
    return order


def _load_graph(db):
    from sqlalchemy import text as T
    posts = [r[0] for r in db.execute(T("SELECT post_id FROM chain_post ORDER BY post_id")).fetchall()]
    edges = [(r[0], r[1]) for r in db.execute(
        T("SELECT from_post_id, to_post_id FROM chain_link")).fetchall()]
    # links are posts too and settle on their own direct pool: put each link right after its parent
    link_parent = {r[1]: r[0] for r in db.execute(
        T("SELECT from_post_id, link_post_id FROM chain_link")).fetchall()}
    edges += [(parent, link) for link, parent in link_parent.items()]
    return posts, edges


def poll_once(db_session_factory) -> None:
    """One cycle: if a new epoch has begun (plus margin) and we haven't passed it, settle."""
    global _last_pass_epoch
    import smax_keeper as sk
    if not sk.is_configured():
        return
    sk._build()
    w3, engine, sign_and_send = sk._state["w3"], sk._state["engine"], sk._state["sign_and_send"]
    now = w3.eth.get_block("latest").timestamp
    current_epoch = now // EPOCH_SEC
    if current_epoch <= _last_pass_epoch:
        return
    if now < current_epoch * EPOCH_SEC + MARGIN_SEC:
        return
    db = db_session_factory()
    try:
        posts, edges = _load_graph(db)
    finally:
        db.close()
    order = topo_order(posts, edges)
    pending = []
    for pid in order:
        try:
            if engine.functions.getLastSnapshotEpoch(pid).call() < current_epoch:
                pending.append(pid)
        except Exception as e:  # pre-Game-B ABI or RPC hiccup: skip this cycle, log once
            logger.warning("settle_keeper: getLastSnapshotEpoch(%d) failed: %s", pid, e)
            return
    _stats["last_lag_posts"] = len(pending)
    if not pending:
        _last_pass_epoch = current_epoch
        return
    logger.info("settle_keeper: epoch %d pass — %d/%d posts to settle (topological order)",
                current_epoch, len(pending), len(order))
    settled = failed = 0
    for pid in pending[:MAX_PER_CYCLE]:
        try:
            tx = engine.functions.updatePost(pid).build_transaction({"from": sk._state["account"].address})
            tx_hash = sign_and_send(tx)
            settled += 1
            logger.info("settle_keeper: settled post=%d tx=%s", pid, tx_hash)
        except Exception as e:
            failed += 1
            logger.error("settle_keeper: updatePost(%d) failed: %s", pid, e)
            _alert(pid, str(e))
    _stats["passes"] += 1
    _stats["settled"] += settled
    _stats["failed"] += failed
    if failed == 0 and len(pending) <= MAX_PER_CYCLE:
        _last_pass_epoch = current_epoch
    logger.info("settle_keeper: epoch %d pass done — settled=%d failed=%d", current_epoch, settled, failed)


def _alert(post_id: int, detail: str):
    try:
        import notify
        notify.send_alert("settle_keeper_failed", f"settlement pass failed for post {post_id}",
                          post_id=post_id, detail=detail[:300])
    except Exception:
        pass
