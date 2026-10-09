#!/usr/bin/env python3
"""app/balance_sampler.py

System-economics instrumentation: samples the native AVAX balance of every
gas-paying EOA (relay, MM, treasury worker) into ops_metrics on a cadence. The
DRAWDOWN of each balance over a period IS that component's gas spend — robust by
construction, because it catches ALL gas regardless of whether any per-tx logger
saw it. (A balance that goes UP is a top-up/deposit, handled in the dashboard by
showing net change and flagging increases.)

Why balance-drawdown rather than per-tx gas logging: there are three gas payers
(relay_wallet, mm_wallet, treasury_worker) on different submission paths, and only
the relay is per-tx logged. Summing logs would undercount. Reading the tank level
cannot undercount.

Runs as an asyncio task inside the worker, mirroring ops_metrics_writer. Wire in
worker.py alongside the other create_task calls:

    import balance_sampler as _bs
    async def _balance_sampler():
        await asyncio.sleep(45)
        while True:
            try:
                _bs.sample_balances_once()
            except Exception as e:
                print(f"balance-sampler error: {e}")
            await asyncio.sleep(_bs.BALANCE_SAMPLE_INTERVAL_SEC)
    asyncio.create_task(_balance_sampler())
    print("balance sampler scheduled")
"""
import logging
import os
import time

from sqlalchemy import text as sql_text

from db import get_session_factory

logger = logging.getLogger(__name__)

BALANCE_SAMPLE_INTERVAL_SEC = int(os.getenv("BALANCE_SAMPLE_INTERVAL_SEC", "300"))  # 5 min default

# Resolve the gas-paying EOAs. MM + TREASURY come from config; relay + worker from env.
try:
    from config import MM_ADDRESS
except Exception:
    MM_ADDRESS = os.getenv("MM_ADDRESS", "")

RELAY_ADDRESS = os.getenv("RELAY_ADDRESS", "")
WORKER_ADDRESS = os.getenv("MM_TREASURY_WORKER_ADDRESS", "")

# patch_ops_watch_wallets: every system wallet, not just the gas payers. Fixed roles come
# from the env the worker already has; anything else (deployer, the three Safe signers)
# is listed in VSP_WATCH_WALLETS="label:0xaddr,label:0xaddr". Safes also get their USDC
# and VSP balances (token_balance{component,token}). Labels are what the dashboard shows.
_FIXED_WALLET_ENVS = (
    ("relay", "RELAY_ADDRESS"),
    ("keeper", "KEEPER_ADDRESS"),
    ("cold_safe", "TREASURY_ADDRESS"),
    ("hot_safe", "HOT_SAFE_ADDRESS"),
    ("guardian_safe", "GUARDIAN_SAFE_ADDRESS"),
    ("cold_reserve", "VSP_COLD_RESERVE_ADDRESS"),
)
_ADDR_RE = __import__("re").compile(r"^0x[0-9a-fA-F]{40}$")


def _parse_watch_wallets(raw: str) -> dict:
    """'label:0xaddr,label2:0xaddr2' -> {label: addr}. Bad entries are logged and skipped,
    never fatal (instrumentation must not break the worker). Labels: [a-z0-9_]."""
    out = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            logger.warning("balance_sampler: VSP_WATCH_WALLETS entry without ':' skipped: %r", item)
            continue
        label, addr = (x.strip() for x in item.split(":", 1))
        if not __import__("re").fullmatch(r"[a-z0-9_]{1,32}", label) or not _ADDR_RE.match(addr):
            logger.warning("balance_sampler: VSP_WATCH_WALLETS entry malformed, skipped: %r", item)
            continue
        out[label] = addr
    return out


def _token_components() -> set:
    """Components whose USDC + VSP balances are sampled too (default: the three Safes)."""
    raw = os.getenv("VSP_WATCH_TOKEN_COMPONENTS", "cold_safe,hot_safe,guardian_safe")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _http_probes() -> list:
    """(service, url) pairs for svc_up. Default = what runs behind the prod edge; override
    with SVC_HTTP_PROBES="name=url,name=url" (dev's edge is nginx, so caddy reads down there)."""
    raw = os.getenv("SVC_HTTP_PROBES",
                    "app=http://app:8070/healthz,caddy=http://caddy/healthz,grafana=http://grafana:3000/api/health")
    out = []
    for item in raw.split(","):
        item = item.strip()
        if "=" in item:
            name, url = item.split("=", 1)
            if name.strip() and url.strip():
                out.append((name.strip(), url.strip()))
    return out

try:
    from config import RPC_READ_URLS
except Exception:
    _u = os.getenv("RPC_URL_READ", os.getenv("RPC_URL", ""))
    RPC_READ_URLS = [_u] if _u else []


def _components():
    """Every system wallet to sample. Label -> address. Unset roles are skipped; the
    MM-era labels stay only while their env is still set. patch_ops_watch_wallets."""
    out = {}
    for label, env in _FIXED_WALLET_ENVS:
        addr = os.getenv(env, "").strip()
        if addr and _ADDR_RE.match(addr):
            out[label] = addr
    if MM_ADDRESS:
        out["mm"] = MM_ADDRESS
    if WORKER_ADDRESS:
        out["worker"] = WORKER_ADDRESS
    for label, addr in _parse_watch_wallets(os.getenv("VSP_WATCH_WALLETS", "")).items():
        out.setdefault(label, addr)
    return out


def _w3():
    from tx_signer import build_w3
    return build_w3(RPC_READ_URLS, require_connected=False)


def record_metric(db, metric, value, labels=None):
    db.execute(sql_text(
        "INSERT INTO ops_metrics (metric, value_num, labels, sampled_at) "
        "VALUES (:m, :v, CAST(:l AS JSONB), now())"
    ), {"m": metric, "v": (float(value) if value is not None else None),
        "l": (None if labels is None else __import__("json").dumps(labels))})


# patch_postreview_circ_growth_alert: F-3 instrumentation (LAUNCH-RISK-AUDIT
# 2026-07-06 §1.2c). Mint-to-winners + the StakeEngine cap exemption means
# circulating supply can grow without an on-chain ceiling, diluting
# floor = reserves/circulating for every holder. Until the supply-redesign
# ruling (deferred to counsel, 2026-07-15) lands, WATCH the growth rate and
# alert when it exceeds a threshold over a trailing window, so dilution is a
# graph you saw coming — not a state you discover in production.
CIRC_GROWTH_ALERT_PCT = float(os.getenv("VSP_CIRC_GROWTH_ALERT_PCT", "20"))
CIRC_GROWTH_WINDOW_HOURS = float(os.getenv("VSP_CIRC_GROWTH_WINDOW_HOURS", "24"))
CIRC_GROWTH_COOLDOWN_SEC = int(os.getenv("VSP_CIRC_GROWTH_COOLDOWN_SEC", "21600"))
_last_circ_growth_alert = 0.0


def _check_circ_growth(db, circ_now) -> None:
    """Alert when circulating VSP grew more than CIRC_GROWTH_ALERT_PCT within
    the trailing window. Baseline = the newest ops_metrics sample at/before the
    window start, so growth is measured over at least the full window. Silently
    skips at bootstrap (no baseline, or baseline == 0). In-process cooldown
    throttles repeats; a persisting condition re-alerts after the cooldown.
    Never raises (instrumentation must not break sampling).
    patch_postreview_circ_growth_alert."""
    global _last_circ_growth_alert
    try:
        if time.time() - _last_circ_growth_alert < CIRC_GROWTH_COOLDOWN_SEC:
            return
        base = db.execute(sql_text(
            "SELECT value_num FROM ops_metrics "
            "WHERE metric = 'vsp_circulating' "
            "  AND sampled_at <= now() - make_interval(secs => :s) "
            "ORDER BY sampled_at DESC LIMIT 1"
        ), {"s": CIRC_GROWTH_WINDOW_HOURS * 3600.0}).scalar()
        if base is None or float(base) <= 0:
            return
        base = float(base)
        growth_pct = (float(circ_now) - base) / base * 100.0
        if growth_pct >= CIRC_GROWTH_ALERT_PCT:
            _last_circ_growth_alert = time.time()
            logger.warning(
                "ALERT circ_growth: circulating VSP +%.1f%% over %gh (%.2f -> %.2f)",
                growth_pct, CIRC_GROWTH_WINDOW_HOURS, base, float(circ_now),
            )
            try:
                import notify
                notify.send_alert(
                    "circ_growth",
                    f"circulating VSP grew {growth_pct:.1f}% in {CIRC_GROWTH_WINDOW_HOURS:g}h "
                    f"({base:.2f} -> {float(circ_now):.2f}) — mint-to-winners dilution watch (F-3)",
                    baseline=base, current=float(circ_now), pct=round(growth_pct, 2),
                )
            except Exception as e:
                logger.warning("circ_growth alert delivery failed: %s", e)
    except Exception as e:
        logger.warning("circ_growth check failed: %s", e)


def sample_balances_once():
    """Read each gas-payer's AVAX balance and record it. One ops_metrics row per
    component, metric='avax_balance', labels={'component': <label>, 'address': <addr>}."""
    comps = _components()
    if not comps:
        logger.warning("balance_sampler: no gas-payer addresses resolved — nothing to sample")
        return {"sampled": 0}
    db = get_session_factory()()
    sampled = 0
    try:
        w3 = _w3()
        for label, addr in comps.items():
            try:
                wei = w3.eth.get_balance(w3.to_checksum_address(addr))
                avax = wei / 1e18
                record_metric(db, "avax_balance", avax,
                              {"component": label, "address": addr})
                sampled += 1
            except Exception as e:
                logger.warning("balance_sampler: %s (%s) read failed: %s", label, addr, e)
        # --- token balances of the Safes (USDC, VSP) --- patch_ops_watch_wallets
        try:
            _erc20 = [{"constant": True, "inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
                       "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}]
            _tokens = []
            _usdc = os.getenv("USDC_ADDRESS", "").strip()
            if _usdc and _ADDR_RE.match(_usdc):
                _tokens.append(("USDC", _usdc, 1e6))
            try:
                from config import VSP_TOKEN_ADDRESS as _vspt
            except Exception:
                _vspt = os.getenv("VSP_TOKEN_ADDRESS", "")
            if _vspt and _ADDR_RE.match(_vspt):
                _tokens.append(("VSP", _vspt, 1e18))
            for label in sorted(_token_components() & set(comps)):
                for tok, taddr, scale in _tokens:
                    try:
                        c = w3.eth.contract(address=w3.to_checksum_address(taddr), abi=_erc20)
                        bal = c.functions.balanceOf(w3.to_checksum_address(comps[label])).call() / scale
                        record_metric(db, "token_balance", bal,
                                      {"component": label, "token": tok, "address": comps[label]})
                        sampled += 1
                    except Exception as e:
                        logger.warning("balance_sampler: %s %s read failed: %s", label, tok, e)
        except Exception as e:
            logger.warning("balance_sampler: token balances failed: %s", e)
        # --- pool-era economic reads (Track B) --- patch_trackb_pool_reader
        # When the public pool is configured, its price IS the market price and
        # vsp_circulating_v2 (totalSupply - company-controlled balances) is the
        # supply figure. Recorded alongside (not instead of) the MM-era metrics
        # until the MM retires, so the two eras overlap on the graphs.
        try:
            from config import POOL_PAIR_ADDRESS
            if POOL_PAIR_ADDRESS:
                from chain.pool_price import read_pool_state, read_vsp_circulating_v2
                _pst = read_pool_state()
                record_metric(db, "pool_price_usdc", float(_pst["price_usdc_per_vsp"]))
                record_metric(db, "pool_vsp_reserve", float(_pst["vsp_reserve"]))
                record_metric(db, "pool_usdc_reserve", float(_pst["usdc_reserve"]))
                record_metric(db, "vsp_circulating_v2", float(read_vsp_circulating_v2()))
                sampled += 4
        except Exception as e:
            logger.warning("balance_sampler: pool reads failed: %s", e)
        # --- MM-era economic reads (retire WITH the MM) --- patch_trackb_pool_reader
        # Gated on the same env flag as the /api/mm kill-line, read directly so the
        # worker never imports the MM/KMS modules. Flipping MM_ROUTES_ENABLED=false
        # retires floor/buy/sell/cap metrics AND the F-3 circ_growth alert in the
        # same motion (the alert lives inside this block by design).
        if os.getenv("MM_ROUTES_ENABLED", "true").strip().lower() != "false":
          try:
            from chain.chain_reader import read_vsp_circulating, read_usdc_reserves
            circ = float(read_vsp_circulating())
            res = float(read_usdc_reserves())
            record_metric(db, "vsp_circulating", circ)
            record_metric(db, "usdc_reserves", res)
            _check_circ_growth(db, circ)  # patch_postreview_circ_growth_alert
            sampled += 2
            # patch_mm_410: floor + MM spot metrics are MM-model outputs; in the
            # pool era they would be fiction (no floor exists, no MM quotes).
            # Pool price is chain truth served by /api/pool/price.
            from config import MM_ROUTES_ENABLED as _mm_on
            if _mm_on:
                # floor = reserves / circulating (liquidation floor; the simple form).
                if circ > 0:
                    record_metric(db, "floor_price_usd", res / circ)
                    sampled += 1
                # current spot sale/buy price (the live MM quote, distinct from floor).
                try:
                    from mm.mm_pricing import get_spot_quote
                    q = get_spot_quote(int(round(circ)), res, circ)
                    record_metric(db, "sell_price_usd", float(q.sell_price_usd))
                    record_metric(db, "buy_price_usd", float(q.buy_price_usd))
                    sampled += 2
                except Exception as e:
                    logger.warning("balance_sampler: spot quote read failed: %s", e)
            # circulating cap = VSPToken.maxAllowedSupply() (time-grown cap on-chain).
            try:
                from config import VSP_TOKEN_ADDRESS
                _maxabi = [{"constant": True, "inputs": [], "name": "maxAllowedSupply",
                            "outputs": [{"name": "", "type": "uint256"}],
                            "stateMutability": "view", "type": "function"}]
                vsp = w3.eth.contract(address=w3.to_checksum_address(VSP_TOKEN_ADDRESS), abi=_maxabi)
                cap = float(vsp.functions.maxAllowedSupply().call()) / 1e18
                record_metric(db, "circulating_cap", cap)
                if cap > 0:
                    record_metric(db, "circulating_headroom", cap - circ)
                sampled += 2
            except Exception as e:
                logger.warning("balance_sampler: cap read failed: %s", e)
          except Exception as e:
            logger.warning("balance_sampler: economic reads failed: %s", e)
        else:
            logger.info("balance_sampler: MM-era metrics retired (MM_ROUTES_ENABLED=false) — pool metrics only")

        # --- health/ops metrics (so the Status + Operations panels populate without
        # needing a separate ops_metrics_writer task) ---
        try:
            record_metric(db, "self_up", 1, {"container": os.getenv("HOSTNAME", "worker")})
            # rpc_up: the w3 used above connected if economic reads ran; probe block number.
            try:
                _ = w3.eth.block_number
                record_metric(db, "rpc_up", 1)
            except Exception:
                record_metric(db, "rpc_up", 0)
            # tx error rate (5m) + pending backlog from tx_log (no host access).
            row = db.execute(sql_text(
                "SELECT "
                "  COUNT(*) FILTER (WHERE status IN ('reverted','dropped') "
                "                   AND COALESCE(resolved_at, submitted_at) > now() - interval '5 minutes') AS err_5m, "
                "  COUNT(*) FILTER (WHERE status = 'pending') AS pending "
                "FROM tx_log"
            )).one()
            record_metric(db, "error_rate_5m", row.err_5m)
            record_metric(db, "pending_tx_count", row.pending)
            # indexer lag: head - last indexed block.
            try:
                head = w3.eth.block_number
                last = db.execute(sql_text(
                    "SELECT value FROM chain_indexer_state WHERE key = 'last_block_global'"
                )).scalar()
                if last is not None:
                    record_metric(db, "indexer_lag_blocks", int(head) - int(last))
            except Exception as e:
                logger.warning("balance_sampler: indexer lag read failed: %s", e)
            # patch_ops_watch_wallets: drift RIGHT NOW (findings in the last audit window),
            # not a 24 h count — the boundary lag at the epoch turn clears within minutes.
            try:
                drift_now = db.execute(sql_text(
                    "SELECT COUNT(*) FROM indexer_audit_log WHERE audited_at > now() - interval '6 minutes'"
                )).scalar()
                record_metric(db, "indexer_audit_drift_now", int(drift_now or 0))
            except Exception as e:
                logger.warning("balance_sampler: audit drift read failed: %s", e)
            # timelock watcher: how far its cursor trails the indexer's head block.
            try:
                row2 = db.execute(sql_text(
                    "SELECT (SELECT value FROM chain_indexer_state WHERE key='last_block_global')::bigint - "
                    "       (SELECT value FROM chain_indexer_state WHERE key='timelock_watch_last_block')::bigint"
                )).scalar()
                if row2 is not None:
                    record_metric(db, "timelock_watch_lag_blocks", int(row2))
            except Exception as e:
                logger.warning("balance_sampler: timelock watcher lag read failed: %s", e)
            # resource levels if psutil present (reads /proc, not the Docker socket).
            try:
                import psutil
                record_metric(db, "cpu_pct", psutil.cpu_percent(interval=None))
                record_metric(db, "mem_pct", psutil.virtual_memory().percent)
            except Exception:
                pass
            sampled += 1
        except Exception as e:
            logger.warning("balance_sampler: health metrics failed: %s", e)

        # --- per-service health probes (Option A: HTTP probes + implicit/DB signals;
        # no Docker socket). Records svc_up{service=...} = 1/0 per service. ---
        try:
            import urllib.request, urllib.error
            def _http_up(url, timeout=4):
                # ANY HTTP response (even 403/404) proves the server is listening = up.
                # Vite's dev server 403s a bare GET (host-header allowlist), but it's up.
                # Only a connection error / timeout / DNS failure means actually down.
                try:
                    with urllib.request.urlopen(url, timeout=timeout):
                        return 1
                except urllib.error.HTTPError:
                    return 1  # server answered with an HTTP status -> it's up
                except Exception:
                    return 0  # connection refused / timeout / DNS -> down
            # patch_ops_watch_wallets: the services that actually run behind the prod edge.
            # HTTP probes by compose DNS name (app, caddy, grafana; overridable via
            # SVC_HTTP_PROBES); postgres and this worker are implicit. The MM-era "frontend"
            # (dev's Vite server) and "treasury_worker" probes are gone with the MM.
            for _name, _url in _http_probes():
                record_metric(db, "svc_up", _http_up(_url), {"service": _name})
            # postgres: implicit — if we got here, our DB session works, so it's up.
            record_metric(db, "svc_up", 1, {"service": "postgres"})
            # main worker: the sampler runs inside it, so reaching this code = up.
            record_metric(db, "svc_up", 1, {"service": "worker"})
            sampled += 1
        except Exception as e:
            logger.warning("balance_sampler: service probes failed: %s", e)

        db.commit()
        return {"sampled": sampled}
    except Exception as e:
        db.rollback()
        logger.warning("balance_sampler sample failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(sample_balances_once())
