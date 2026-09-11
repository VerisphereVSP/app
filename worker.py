#!/usr/bin/env python3
"""Standalone background worker for chain indexing, article refresh, and dupe groups.
Runs as a separate process so it never blocks the API server."""

import asyncio
import time
import sys
import os
import logging

# patch05a: enable INFO-level logging so logger.info / logger.warning
# calls from dupe_groups, chain_indexer, and other modules are
# visible in `docker compose logs worker`. Without this, Python's
# default WARNING-only level silently dropped all operational
# logging, making debugging extremely difficult.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

# Ensure app directory is in path
sys.path.insert(0, os.path.dirname(__file__))

logger = logging.getLogger(__name__)

# patch_bundle08_idle_tx_alert: surface orphaned "idle in transaction" sessions
# BEFORE the postgres idle_in_transaction_session_timeout (Bundle 3, 5min)
# reaps them — so the offending connection is still attached and its last query
# is visible, making the leak source diagnosable. Companion to that timeout, not
# a replacement. Thresholds env-tunable.
IDLE_TX_ALERT_THRESHOLD_SEC = int(os.getenv("IDLE_TX_ALERT_THRESHOLD_SEC", "60"))
IDLE_TX_ALERT_INTERVAL_SEC = int(os.getenv("IDLE_TX_ALERT_INTERVAL_SEC", "60"))


def check_idle_in_transaction(threshold_sec: int = IDLE_TX_ALERT_THRESHOLD_SEC):
    """Read-only scan of pg_stat_activity for sessions stuck 'idle in transaction'
    longer than threshold_sec. Emits one structured ALERT per offender and
    returns the offending rows. Opens AND closes its own connection within the
    call — it must never hold a transaction across the monitor's sleep, or it
    would become the very leak it is meant to detect. (Same-role sessions show
    full query text; other roles may show empty query — fine, the leak we care
    about is app/worker, same role.)"""
    from db import get_engine
    from sqlalchemy import text
    sql = text(
        """
        SELECT pid,
               EXTRACT(EPOCH FROM (now() - state_change)) AS idle_seconds,
               coalesce(usename, '')           AS usename,
               coalesce(application_name, '')  AS application_name,
               left(coalesce(query, ''), 200)  AS last_query
        FROM pg_stat_activity
        WHERE state = 'idle in transaction'
          AND state_change IS NOT NULL
          AND EXTRACT(EPOCH FROM (now() - state_change)) > :thr
        ORDER BY idle_seconds DESC
        """
    )
    eng = get_engine()
    with eng.connect() as conn:
        rows = list(conn.execute(sql, {"thr": float(threshold_sec)}))
    for r in rows:
        logger.warning(
            "ALERT idle_in_transaction: pid=%s idle=%.0fs user=%s app=%s last_query=%r",
            r.pid, r.idle_seconds, r.usename, r.application_name, r.last_query,
        )
        # patch_bundle08_alert_sink: fan out to the notifier (no-op if unconfigured).
        try:
            import notify
            notify.send_alert(
                "idle_in_transaction",
                f"pid {r.pid} idle in transaction {r.idle_seconds:.0f}s",
                pid=r.pid, idle_seconds=round(r.idle_seconds, 0),
                app=r.application_name,
            )
        except Exception:
            pass
    return rows


async def main():
    print("=== Verisphere Background Worker ===")

    # patch04a: legacy indexer disabled.
    # app/chain/indexer.py is kept on disk for reference but is no
    # longer instantiated — its _sync_posts loop iterates from
    # pid=0 (off-by-one) and races with the main indexer's
    # claim-row update path, producing rows with claim.post_id=0.
    # The main indexer in chain_indexer.py covers all of its
    # functionality plus more.
    # patch_bundle04_atomic: start derived-state worker alongside indexer.
    # Indexer writes canonical chain state synchronously; derived-state
    # worker chews through the slow article-system/dupe-grouping/topic
    # detection work asynchronously from derived_state_queue.
    from chain_indexer import start_indexer
    from derived_state_worker import start_derived_state_worker

    start_indexer()
    print("Chain indexer started (legacy run_indexer disabled by patch04a)")

    start_derived_state_worker()
    print("Derived-state worker started")

    # patch_bundle04_p2: periodic drift-check between DB and chain.
    from indexer_audit import start_indexer_audit
    start_indexer_audit()
    print("Indexer audit started")

    # Periodic dupe group refresh (every 5 minutes)
    async def _dupe_refresh():
        await asyncio.sleep(180)  # Initial delay
        while True:
            try:
                from db import get_session_factory
                from dupe_groups import sweep_unassigned_groups  # patch_group_global: non-destructive sweep
                sess = get_session_factory()()
                try:
                    sweep_unassigned_groups(sess)
                finally:
                    sess.close()
            except Exception as e:
                print(f"Dupe group refresh error: {e}")
            await asyncio.sleep(300)

    asyncio.create_task(_dupe_refresh())
    print("Dupe group refresh scheduled")

    # patch_bundle04_atomic: _topic_backfill removed.
    # full_sync at startup now enqueues derived-state work for every
    # claim post; the derived-state worker handles topic detection
    # uniformly with the rest of the derivation pipeline. No separate
    # backfill task needed.

    # Background article refresh
    async def _daily_refresh():
        import statistics
        from db import get_session_factory
        from articles.article_store import refresh_article, persist_dedup, build_and_cache_response
        from sqlalchemy import text as sql_text
        CYCLE_SECONDS = 86400  # 24h target
        recent_elapsed = []
        await asyncio.sleep(120)  # Initial delay
        # patch_bundle04_5_p7_worker_session_leak: session must be opened, used, and closed
        # ENTIRELY BEFORE any `await asyncio.sleep(...)`. Holding a session
        # across an async sleep, while it has an open (implicit) transaction
        # from a prior SELECT, is what produced the idle-in-transaction
        # leak on `SELECT count(*) FROM topic_article` (bounded by the 5-min
        # idle_in_transaction_session_timeout, but leaking real connections
        # every cycle until then).
        while True:
            try:
                # Compute next sleep INSIDE the DB-work block and use it
                # AFTER db.close(). The two paths (no-article vs. refreshed)
                # set `next_sleep` independently; we sleep once below.
                next_sleep = 60  # default if anything unexpected
                Sess = get_session_factory()
                db = Sess()
                try:
                    row = db.execute(sql_text(
                        "SELECT article_id, topic_key FROM topic_article "
                        "ORDER BY last_refreshed_at ASC NULLS FIRST LIMIT 1"
                    )).fetchone()
                    if not row:
                        # No articles yet — just poll again in 60s.
                        next_sleep = 60
                    else:
                        aid, topic = row
                        t0 = time.time()
                        refresh_article(db, topic)
                        persist_dedup(db, aid)
                        build_and_cache_response(db, topic)
                        elapsed = time.time() - t0
                        recent_elapsed.append(elapsed)
                        if len(recent_elapsed) > 20:
                            recent_elapsed = recent_elapsed[-20:]
                        total = db.execute(sql_text(
                            "SELECT count(*) FROM topic_article"
                        )).scalar() or 1
                        avg = statistics.mean(recent_elapsed) if recent_elapsed else 30
                        gap = max((CYCLE_SECONDS / total) - avg, 5)
                        print(f"Refreshed article '{topic}' in {elapsed:.1f}s, next in {gap:.0f}s")
                        next_sleep = gap
                    # Commit closes any implicit transaction opened by the
                    # SELECTs above. The article-store helpers above commit
                    # their own writes; this catches the read-only SELECTs.
                    db.commit()
                finally:
                    db.close()
                # Session is closed here. Safe to sleep without holding
                # an idle-in-transaction session.
                await asyncio.sleep(next_sleep)
            except Exception as e:
                print(f"Article refresh error: {e}")
                await asyncio.sleep(60)

    asyncio.create_task(_daily_refresh())
    print("Article refresh scheduled")

    # patch_topicart_reconcile_task: self-healing one-article-per-topic. Collapses
    # any topic with >1 article to a single canonical article (claims preserved,
    # deletions logged+alerted). Keyed on topic_id, never on name — asserts the
    # topic count is unchanged so meanings are never fused.
    async def _topic_article_reconcile():
        await asyncio.sleep(90)
        import os as _os
        from db import get_session_factory as _sf
        from articles.topic_article_canonical import reconcile_topic_articles as _rec
        _interval = int(_os.getenv("VSP_TOPIC_ARTICLE_RECONCILE_SEC", "3600"))
        while True:
            try:
                _db = _sf()()
                try:
                    _res = _rec(_db)
                    if _res.get("collapsed") or _res.get("errors"):
                        print(f"topic-article-reconcile: {_res}", flush=True)
                finally:
                    _db.close()
            except Exception as _e:
                print(f"topic-article-reconcile error: {_e}", flush=True)
            await asyncio.sleep(_interval)
    asyncio.create_task(_topic_article_reconcile())
    print("Topic-article reconciler scheduled")

    # patch_bundle08_idle_tx_alert: periodic idle-in-transaction monitor.
    async def _idle_tx_monitor():
        await asyncio.sleep(45)  # initial delay; let startup transactions settle
        print(
            f"Idle-in-transaction monitor started "
            f"(threshold={IDLE_TX_ALERT_THRESHOLD_SEC}s, interval={IDLE_TX_ALERT_INTERVAL_SEC}s)"
        )
        while True:
            try:
                check_idle_in_transaction(IDLE_TX_ALERT_THRESHOLD_SEC)
            except Exception as e:
                print(f"idle-tx monitor error: {e}")
            await asyncio.sleep(IDLE_TX_ALERT_INTERVAL_SEC)

    asyncio.create_task(_idle_tx_monitor())
    print("Idle-in-transaction monitor scheduled")

    # patch_bundle08_timelock_watcher: poll the on-chain TimelockController for
    # governance events (scheduled/executed/cancelled calls, role + min-delay
    # changes) and fan them out via notify.send_alert. Read-only; no-ops cleanly
    # until a TimelockController address is present in deployments AND governance
    # is moved onto it (Bundle 12). Mirrors the idle-tx monitor's task shape.
    import timelock_watcher as _tlw

    async def _timelock_watcher():
        await asyncio.sleep(50)  # initial delay; let chain/db settle past startup
        if _tlw.TIMELOCK_ADDRESS:
            print(f"Timelock watcher started (timelock={_tlw.TIMELOCK_ADDRESS}, "
                  f"interval={_tlw.TIMELOCK_WATCH_INTERVAL_SEC}s)")
        else:
            print("Timelock watcher: no TimelockController in deployments - "
                  "watcher idle (expected on Fuji pre-Bundle-12)")
        while True:
            try:
                _tlw.poll_once()
            except Exception as e:
                print(f"timelock watcher error: {e}")
            await asyncio.sleep(_tlw.TIMELOCK_WATCH_INTERVAL_SEC)

    asyncio.create_task(_timelock_watcher())
    print("Timelock watcher scheduled")

    # patch_wire_balance_sampler_loop: periodic balance/economics/health sampler.
    # Was previously only ever run manually -> dashboard went stale unattended.
    # try/except INSIDE the loop so one bad sample can't silently kill the task.
    async def _balance_sampler():
        await asyncio.sleep(45)  # initial delay; let startup settle
        import os as _os
        import balance_sampler as _bs
        interval = int(_os.getenv("BALANCE_SAMPLE_INTERVAL_SEC", "300"))
        while True:
            try:
                _bs.sample_balances_once()
            except Exception as e:
                print(f"balance-sampler error: {e}", flush=True)
            await asyncio.sleep(interval)
    asyncio.create_task(_balance_sampler())
    print("balance sampler scheduled", flush=True)

    # patch_f7rg_reconcile_task: F-7 partial-trade reconciler. Finds trades where
    # one leg landed but a later leg failed, and completes-forward or refunds-back
    # (fund movement gated by MM_RECONCILE_ENABLED, default ON). try/except INSIDE
    # the loop so one bad pass can't kill the task. Interval independent of the
    # sampler so cure latency is tunable without touching metrics cadence.
    async def _mm_reconcile_loop():
        await asyncio.sleep(60)  # let startup settle; after the sampler's 45s
        import os as _os
        from mm import mm_reconcile as _mr
        _interval = int(_os.getenv("MM_RECONCILE_INTERVAL_SEC", "120"))
        while True:
            try:
                _res = _mr.reconcile_once()
                if _res and any(_res.get(k) for k in ("reconciled", "refunded", "failed")):
                    print(f"mm-reconcile: {_res}", flush=True)
            except Exception as _e:
                print(f"mm-reconcile error: {_e}", flush=True)
            await asyncio.sleep(_interval)
    _mm_on = os.getenv("MM_ROUTES_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    if _mm_on:
        asyncio.create_task(_mm_reconcile_loop())
        print("mm reconciler scheduled", flush=True)
    else:
        print("mm reconciler disabled (MM_ROUTES_ENABLED=false — pool era)", flush=True)

    # patch_mm_410: sMax keeper wiring (S-03 layer ii operational half). The
    # module self-gates: KEEPER_KMS_KEY/KEEPER_ADDRESS unset -> logs and idles,
    # so this is inert until the keeper key ceremony. poll_once() is sync
    # (web3 + KMS) — run it off the event loop.
    import smax_keeper as _sk
    if _sk.is_configured():
        async def _keeper_loop():
            await asyncio.sleep(90)  # let indexer/env settle first
            while True:
                try:
                    await asyncio.to_thread(_sk.poll_once)
                except Exception as _e:
                    print(f"smax-keeper error: {_e}", flush=True)
                await asyncio.sleep(_sk.KEEPER_INTERVAL_SEC)
        asyncio.create_task(_keeper_loop())
        print(f"smax keeper scheduled (interval {_sk.KEEPER_INTERVAL_SEC}s, addr {_sk.keeper_address()})", flush=True)
    else:
        print("smax keeper: unconfigured (KEEPER_KMS_KEY/KEEPER_ADDRESS unset) — idle", flush=True)

    # patch_phase5: EMISSION TRIPWIRE (founder ruling 2026-09-10, review-3 #5).
    # The StakeEngine is exempt from the supply cap by design (it mints staking
    # rewards at settlement), so the blast-radius control for a compromised or
    # buggy engine is the guardian PAUSE, which halts settlements — the only
    # mint path. This loop watches totalSupply and pages the guardian when
    # 24h net emission exceeds EMISSION_ALERT_VSP_PER_DAY (default 100,000 VSP;
    # tune to ~2x max APY on staked supply). Read-only; never acts on chain.
    async def _emission_tripwire():
        import os as _os, time as _time
        from collections import deque as _dq
        from web3 import Web3 as _W3
        budget = float(_os.getenv("EMISSION_ALERT_VSP_PER_DAY", "100000")) * 1e18
        hist = _dq()
        alerted_at = 0.0
        await asyncio.sleep(120)
        while True:
            try:
                from chain.provider import w3 as _w3
                from config import VSP_TOKEN_ADDRESS as _vsp
                tok = _w3.eth.contract(address=_W3.to_checksum_address(_vsp), abi=[{"type":"function","name":"totalSupply","inputs":[],"outputs":[{"type":"uint256"}],"stateMutability":"view"}])
                now = _time.time(); supply = int(tok.functions.totalSupply().call())
                hist.append((now, supply))
                while hist and hist[0][0] < now - 86400:
                    hist.popleft()
                delta = supply - hist[0][1]
                if delta > budget and now - alerted_at > 3600:
                    alerted_at = now
                    import notify
                    notify.send_alert("emission_tripwire",
                        f"VSP net emission {delta/1e18:,.0f} in the last 24h exceeds budget {budget/1e18:,.0f} — "
                        f"if unexpected, guardian: pause the StakeEngine (settlements are the only mint path).",
                        supply=supply, delta_24h=delta)
                    print(f"emission tripwire: ALERT delta24h={delta/1e18:,.0f} VSP", flush=True)
            except Exception as _e:
                print(f"emission tripwire error: {_e}", flush=True)
            await asyncio.sleep(600)
    asyncio.create_task(_emission_tripwire())
    print("emission tripwire scheduled (10m cadence, 24h window)", flush=True)

    # patch_smax_keeper: S-03 layer (ii) operational half. Pokes core's
    # permissionless StakeEngine.refreshSMax when a transaction would actually
    # change state (I.4 deviation, or pending decay once per epoch); view calls
    # only otherwise. Idles cleanly until KEEPER_KMS_KEY/KEEPER_ADDRESS are
    # provisioned. Rationale, cost model, and rules: smax_keeper.py header.
    import smax_keeper as _smk

    async def _smax_keeper_loop():
        await asyncio.sleep(55)  # initial delay; after startup + indexer settle
        if _smk.is_configured():
            print(f"sMax keeper started (keeper={_smk.keeper_address()}, "
                  f"interval={_smk.KEEPER_INTERVAL_SEC}s, "
                  f"top {_smk.KEEPER_CANDIDATES} posts, "
                  f"max {_smk.KEEPER_MAX_POKES_PER_CYCLE} pokes/cycle)")
        else:
            print("sMax keeper: KEEPER_KMS_KEY/KEEPER_ADDRESS not set - keeper "
                  "idle (provision like the RELAY/MM keys to enable; see "
                  "smax_keeper.py header)")
        while True:
            try:
                _smk.poll_once()
            except Exception as e:
                print(f"smax keeper error: {e}", flush=True)
            await asyncio.sleep(_smk.KEEPER_INTERVAL_SEC)

    asyncio.create_task(_smax_keeper_loop())
    print("sMax keeper scheduled", flush=True)

    # patch04b: keep-alive loop. The main indexer runs in a native
    # thread via start_indexer() and doesn't need to be awaited.
    # We just need to keep the asyncio event loop alive so the
    # background tasks scheduled above (dupe_refresh, daily_refresh)
    # can run their initial sleeps and cycles.
    # patch_bundle10d_compose_hardening_worker: heartbeat-file touch for the compose healthcheck.
    # 30s cadence; compose healthcheck threshold is 90s. Touching is
    # cheap and reaches /tmp (tmpfs in slim images), so no I/O concern.
    from pathlib import Path as _HB_Path
    _HB_FILE = _HB_Path("/tmp/worker.heartbeat")
    try:
        while True:
            try:
                _HB_FILE.touch()
            except Exception as _hb_err:
                print(f"worker heartbeat touch failed: {_hb_err}")
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        print("Worker shutting down")

if __name__ == "__main__":
    asyncio.run(main())
