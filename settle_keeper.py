"""settle_keeper.py — epoch settlement pass (whitepaper v18 §3.2, §4.2.6, §5.4).

patch_settlement_snapshots: settlement reads STORED SNAPSHOTS. Each post's own settlement
writes its snapshot (T, vs, epoch); a claim's pool is assembled from the snapshots of its
incoming links and their parents, one hop per epoch. The keeper therefore settles every post
once per epoch in KEEPER ORDER — claims, then the links that hang off them, then the claims
those links point to — so that a child's settlement reads snapshots written moments earlier in
the same pass (topo_order over parent -> link -> child; cycles fall back to id order, which the
contract tolerates: a stale hop lags one epoch, it never recurses). So:
  * money never waits on a user transaction (G7: daily settlement = daily compounding),
  * evidence counts the epoch after it is placed, as the whitepaper says it does,
  * SettleFirst on the user path is rare: it fires only when a counted parent or link snapshot
    is older than the previous epoch (a missed pass) or a claim is unseeded right after the
    upgrade — repair_async() settles that post's one hop on demand (claims, links, post) so the
    user's retry succeeds.

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
    """Kahn's algorithm over the dependency edges (parent -> link, link -> child). Cycles:
    remaining nodes appended in id order (v18: a hop settled out of order reads last epoch's
    snapshot — one epoch of lag, never a revert on the keeper path)."""
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


def dependency_edges(links: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    """(from_post_id, to_post_id, link_post_id) rows -> keeper-order edges parent -> link -> child.
    patch_settlement_snapshots: the child depends on the LINK's snapshot as much as the parent's
    (link share T_l / outSum, link VS), so the link must settle before the child, not merely
    after its parent."""
    edges = []
    for parent, child, link in links:
        edges.append((parent, link))
        edges.append((link, child))
    return edges


def _load_graph(db):
    from sqlalchemy import text as T
    posts = [r[0] for r in db.execute(T("SELECT post_id FROM chain_post ORDER BY post_id")).fetchall()]
    links = [(r[0], r[1], r[2]) for r in db.execute(
        T("SELECT from_post_id, to_post_id, link_post_id FROM chain_link")).fetchall()]
    return posts, dependency_edges(links)


def one_hop(db, post_id: int) -> list[int]:
    """The posts whose snapshots `post_id`'s settlement reads, in keeper order: the parents of its
    incoming links, then those links, then the post itself. What repair_async settles."""
    from sqlalchemy import text as T
    rows = db.execute(T("SELECT from_post_id, link_post_id FROM chain_link WHERE to_post_id = :p ORDER BY link_post_id"),
                      {"p": int(post_id)}).fetchall()
    parents = sorted({int(r[0]) for r in rows})
    links = [int(r[1]) for r in rows]
    return parents + links + [int(post_id)]


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
    logger.info("settle_keeper: epoch %d pass — %d/%d posts to settle (keeper order: claims, links, children)",
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


# ── on-demand repair (user path hit SettleFirst) ────────────────────────────────────────────
_repair_last: dict[int, float] = {}
REPAIR_COOLDOWN_SEC = int(os.getenv("SETTLE_REPAIR_COOLDOWN_SEC", "60"))


def repair_async(db_session_factory, post_id: int) -> bool:
    """Settle `post_id`'s one hop (parents, links, post) from the keeper account, in a thread, at
    most once per REPAIR_COOLDOWN_SEC per post. Returns True when a repair was started. The
    keeper path (updatePost) always completes: stale snapshots are used and StaleParentUsed is
    emitted, so after this the user's retry settles inline. No-op when the keeper is not
    configured (the relay message already tells the user to retry)."""
    import threading
    now = time.time()
    if now - _repair_last.get(post_id, 0.0) < REPAIR_COOLDOWN_SEC:
        return False
    _repair_last[post_id] = now
    try:
        import smax_keeper as sk
        if not sk.is_configured():
            return False
    except Exception:
        return False

    def _run():
        try:
            import smax_keeper as sk2
            sk2._build()
            engine, sign_and_send = sk2._state["engine"], sk2._state["sign_and_send"]
            current_epoch = sk2._state["w3"].eth.get_block("latest").timestamp // EPOCH_SEC
            db = db_session_factory()
            try:
                hop = one_hop(db, post_id)
            finally:
                db.close()
            for pid in hop:
                if engine.functions.getLastSnapshotEpoch(pid).call() >= current_epoch:
                    continue
                tx = engine.functions.updatePost(pid).build_transaction({"from": sk2._state["account"].address})
                tx_hash = sign_and_send(tx)
                _stats["settled"] += 1
                logger.info("settle_keeper: repair for post=%d settled post=%d tx=%s", post_id, pid, tx_hash)
        except Exception as e:
            logger.error("settle_keeper: repair for post=%d failed: %s", post_id, e)
            _alert(post_id, "repair: " + str(e))

    threading.Thread(target=_run, name=f"settle-repair-{post_id}", daemon=True).start()
    return True


def _alert(post_id: int, detail: str):
    try:
        import notify
        notify.send_alert("settle_keeper_failed", f"settlement pass failed for post {post_id}",
                          post_id=post_id, detail=detail[:300])
    except Exception:
        pass


def seed_order(db) -> list[int]:
    """patch_settlement_snapshots: the SEED_ORDER for script/UpgradeSnapshots.s.sol — every CLAIM in
    keeper order (the script seeds links first on its own). Parents before children means a child's
    seed already reads its parents' seeds, so display is exact from the first block after the upgrade."""
    posts, edges = _load_graph(db)
    from sqlalchemy import text as T
    link_ids = {int(r[0]) for r in db.execute(T("SELECT link_post_id FROM chain_link")).fetchall()}
    return [p for p in topo_order(posts, edges) if p not in link_ids]


if __name__ == "__main__":  # python settle_keeper.py seed-order  -> comma-separated claim ids
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "seed-order":
        from db import get_session_factory
        _db = get_session_factory()()
        try:
            print(",".join(str(p) for p in seed_order(_db)))
        finally:
            _db.close()
    else:
        print("usage: python settle_keeper.py seed-order", file=sys.stderr)
        sys.exit(2)
