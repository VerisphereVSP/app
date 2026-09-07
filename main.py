# app/main.py
# patch_bundle03_logging: ensure logger.info from relay.py, article_routes.py,
# etc. is visible in 'docker compose logs app'. Mirrors worker.py.
import os  # patch_bundle12_docs_gate
import logging as _bundle03_logging
import sys as _bundle03_sys
_bundle03_logging.basicConfig(
    level=_bundle03_logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=_bundle03_sys.stdout,
)

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from lang_detect import detect_language, lang_instruction, is_rtl
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path
import json
import json

from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy import text as sql_text

from db import get_db
from config import USDC_ADDRESS, VSP_ADDRESS, FORWARDER_ADDRESS, DIRECT_MM_SIGNING_ENABLED, CHAIN_ID
from semantic import compute_one
from chain.claim_registry import create_claim
from chain.stake import stake_claim
from relay import router as relay_router
from notifications import router as notifications_router  # patch_bundle04a_notifications_mount
from supersedes import router as supersedes_router
from config import MM_ROUTES_ENABLED  # patch_mm_410
if MM_ROUTES_ENABLED:
    from mm.mm_routes import router as mm_router
from claim_views import router as claim_views_router
from portfolio_views import router as portfolio_router
from articles.article_routes import router as article_router
from semantic_dedup import router as semantic_dedup_router
from claim_locate import router as claim_locate_router
from claim_atomicity import router as claim_atomicity_router
from relay_gateway import router as relay_gateway_router
from rate_limit import RateLimitMiddleware, cleanup_rate_limiter, _client_ip as _rl_client_ip, TRUSTED_PROXY_HOPS as _TRUSTED_PROXY_HOPS


# patch_bundle10c_backend_hardening_main: startup CHAIN_ID consistency check.
# Asserts that web3.eth.chain_id == CHAIN_ID and that CHAIN_ID matches
# the NETWORK label. Catches operator footguns where mainnet vars are
# combined with a Fuji RPC URL (or vice versa).
def _assert_chain_id_consistency():
    from config import CHAIN_ID, NETWORK, RPC_URL, RPC_READ_URLS
    from web3 import Web3
    from tx_signer import build_w3  # patch_bundle10_rpc_failover_p2
    import time as _t
    expected_for_label = {
        "fuji": 43113,
        "mainnet": 43114,
    }
    if NETWORK in expected_for_label and expected_for_label[NETWORK] != CHAIN_ID:
        raise RuntimeError(
            f"CHAIN_ID/NETWORK mismatch: NETWORK={NETWORK!r} expects "
            f"CHAIN_ID={expected_for_label[NETWORK]} but got {CHAIN_ID}"
        )
    if not RPC_URL:
        print("chain_id check: RPC_URL is empty; skipping live RPC probe (Fuji-only path)")
        return
    last_err = None
    for attempt, backoff in enumerate([1, 2, 4]):
        try:
            w3 = build_w3(RPC_READ_URLS, require_connected=False)
            live = w3.eth.chain_id
            if live != CHAIN_ID:
                raise RuntimeError(
                    f"chain_id mismatch: RPC reports {live} but config says {CHAIN_ID} "
                    f"(NETWORK={NETWORK!r}). Refusing to start."
                )
            print(f"chain_id check: OK (RPC={live} == CHAIN_ID={CHAIN_ID}, NETWORK={NETWORK!r})")
            return
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            print(f"chain_id check attempt {attempt+1}/3 failed: {e}; sleeping {backoff}s")
            _t.sleep(backoff)
    raise RuntimeError(
        f"chain_id check: RPC unreachable after 3 attempts (last error: {last_err}). "
        f"Refusing to start — startup must verify chain identity."
    )


@asynccontextmanager
async def lifespan(app):
    # patch_bundle10c_backend_hardening_main: assert chain_id matches CHAIN_ID before any
    # background tasks start. Crashes the app loud if not.
    _assert_chain_id_consistency()
    # Background tasks (indexer, article refresh, dupe groups) run in
    # the separate worker service — see worker.py and docker-compose.yml.
    print("API server started (background tasks run in worker service)")
    # Periodic rate limiter cleanup
    import asyncio as _aio
    async def _rl_cleanup():
        while True:
            await _aio.sleep(600)
            cleanup_rate_limiter()
    _aio.create_task(_rl_cleanup())

    # Dupe refresh runs in worker service

    # patch_remove_app_refresh: article refresh removed from the API process — it runs in
    # worker.py (with the session-leak fix this copy lacked). The app is now
    # free of singleton background jobs and is horizontally scalable.

    yield
    print("API server stopped")


from chain_indexer import start_indexer

_expose_docs = os.getenv("EXPOSE_API_DOCS", "").strip().lower() in ("1", "true", "yes")  # patch_bundle12_docs_gate
app = FastAPI(  # patch_bundle12_docs_gate
    title="VeriSphere App API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _expose_docs else None,
    redoc_url="/redoc" if _expose_docs else None,
    openapi_url="/openapi.json" if _expose_docs else None,
)


# patch_public_hardening: path-scoped permissive CORS for registered public endpoints
from starlette.middleware.base import BaseHTTPMiddleware as _PubBHM
from starlette.responses import Response as _PubResp
import rate_limit as _pub_rl


class PublicCORSMiddleware(_PubBHM):
    """Add permissive CORS to exactly the paths in rate_limit.PUBLIC_CORS_PATHS
    (browser-extension access). Handles OPTIONS preflight and adds headers to
    success AND error responses. Leaves all other endpoints same-origin-only."""
    async def dispatch(self, request, call_next):
        if request.url.path in _pub_rl.PUBLIC_CORS_PATHS:
            if request.method == "OPTIONS":
                resp = _PubResp(status_code=204)
            else:
                resp = await call_next(request)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
            resp.headers["Access-Control-Max-Age"] = "86400"
            return resp
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(PublicCORSMiddleware)  # outermost: wraps rate-limit 429s too

app.include_router(relay_router)
app.include_router(notifications_router)
app.include_router(supersedes_router)
if MM_ROUTES_ENABLED:
    app.include_router(mm_router)
else:
    # patch_mm_410: MM retired. Every /api/mm/* answers 410 Gone with a pointer
    # at the public pool, so stale FE bundles and API users get a truthful,
    # machine-readable answer instead of a 404 that looks like a routing bug.
    from fastapi.responses import JSONResponse
    from config import SWAP_URL as _swap_url

    @app.api_route("/api/mm/{_path:path}",
                   methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def _mm_retired(_path: str):
        return JSONResponse(status_code=410, content={
            "detail": "MM retired — trading executes on the public pool from your own wallet",
            "swap_url": _swap_url or None,
        })
app.include_router(claim_views_router)
app.include_router(portfolio_router)
app.include_router(article_router)
app.include_router(semantic_dedup_router)
# Endpoints the VeriSphere browser extension needs (they replace the verity-api
# gateway): article -> claim matching, the atomicity check, and wallet signing
# config + calldata.
app.include_router(claim_locate_router)
app.include_router(claim_atomicity_router)
app.include_router(relay_gateway_router)

ADDRESSES_PATH = Path(f"/app/broadcast/Deploy.s.sol/{CHAIN_ID}/addresses.json")



# ── Admin auth (OPS-03: audit logging + IP allowlist) ──────────────────────────
import os as _os
ADMIN_API_KEY = _os.getenv("ADMIN_API_KEY", "")
# Comma-separated list of allowed IPs. Empty = allow all (but still require key).
ADMIN_IP_ALLOWLIST = [ip.strip() for ip in _os.getenv("ADMIN_IP_ALLOWLIST", "").split(",") if ip.strip()]

# patch_followup_proxyhops_guard: TRUSTED_PROXY_HOPS (rate_limit) and
# ADMIN_IP_ALLOWLIST share the _client_ip resolver. Trusting a proxy hop while an
# admin allowlist is set silently changes which IP the allowlist checks (XFF-
# derived vs socket peer) — a lockout / spoof risk. Refuse the dangerous combo
# loudly at startup.
if _TRUSTED_PROXY_HOPS > 0 and ADMIN_IP_ALLOWLIST:
    raise RuntimeError(
        f"Unsafe config: TRUSTED_PROXY_HOPS={_TRUSTED_PROXY_HOPS} with a non-empty "
        f"ADMIN_IP_ALLOWLIST ({ADMIN_IP_ALLOWLIST}). The admin allowlist and the rate "
        f"limiter share _client_ip; enabling proxy-hop trust changes which IP the "
        f"allowlist checks. Clear ADMIN_IP_ALLOWLIST, or set it to the real client "
        f"IPs and remove this guard deliberately."
    )


def _get_client_ip(request) -> str:
    # patch_bundle06_xff_trusted_proxy: delegate to the single canonical,
    # topology-aware resolver in rate_limit so the admin IP allowlist and
    # the rate limiter agree on one X-Forwarded-For trust rule.
    if not request:
        return "unknown"
    return _rl_client_ip(request)


def _log_admin_action(db, action: str, params: dict, request):
    """Write to admin_audit_log table."""
    try:
        ip = _get_client_ip(request)
        key = request.headers.get("X-Admin-Key", "")[:8] if request else ""
        db.execute(sql_text(
            "INSERT INTO admin_audit_log (action, params, ip_address, admin_key_prefix) "
            "VALUES (:a, CAST(:p AS jsonb), :ip, :kp)"
        ), {"a": action, "p": json.dumps(params), "ip": ip, "kp": key})
        db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Admin audit log failed: %s", e)


def require_admin(request, db=None, action=None, params=None):
    """Check X-Admin-Key header, enforce IP allowlist, log action."""
    if not ADMIN_API_KEY:
        raise HTTPException(403, "Admin API key not configured. Set ADMIN_API_KEY env var.")

    # IP allowlist check
    if ADMIN_IP_ALLOWLIST:
        ip = _get_client_ip(request)
        if ip not in ADMIN_IP_ALLOWLIST and ip != "127.0.0.1":
            raise HTTPException(403, "Admin access denied from this IP.")

    key = request.headers.get("X-Admin-Key", "")
    # security review 2026-09 (Low): constant-time compare
    import hmac as _hmac
    if not _hmac.compare_digest(key.encode(), ADMIN_API_KEY.encode()):
        raise HTTPException(403, "Invalid admin key")

    # Audit log
    if db and action:
        _log_admin_action(db, action, params or {}, request)

@app.get("/healthz")
def healthz():
    return {"ok": "true"}



@app.get("/api/fees")
def get_fees(db: Session = Depends(get_db)):
    """Full fee schedule with cost breakdown and examples."""
    from fee_calculator import get_fee_schedule
    return get_fee_schedule(db)

@app.get("/api/fees/estimate")
def estimate_fee(tx_type: str, value_vsp: float = 1.0, db: Session = Depends(get_db)):
    """Estimate fee for a specific transaction type and value."""
    from fee_calculator import compute_fee
    return compute_fee(db, tx_type, value_vsp)

@app.post("/api/fees/costs")
def update_cost(cost_key: str, monthly_usd: float, request: Request = None, db: Session = Depends(get_db)):
    require_admin(request, db=db, action="update_cost", params={"cost_key": cost_key, "monthly_usd": monthly_usd})
    """Update an operating cost (admin). Fee recalculates automatically."""
    from fee_calculator import invalidate_cache
    db.execute(sql_text(
        "UPDATE operating_costs SET monthly_usd = :usd, updated_at = NOW() WHERE cost_key = :key"
    ), {"key": cost_key, "usd": monthly_usd})
    db.commit()
    invalidate_cache()
    return {"ok": True}

@app.post("/api/fees/params")
def update_fee_param(param_key: str, value: str, request: Request = None, db: Session = Depends(get_db)):
    require_admin(request, db=db, action="update_fee_param", params={"param_key": param_key, "value": value})
    """Update a fee parameter (admin). Fee recalculates automatically."""
    from fee_calculator import invalidate_cache
    db.execute(sql_text(
        "UPDATE fee_params SET value = :val, updated_at = NOW() WHERE param_key = :key"
    ), {"key": param_key, "val": value})
    db.commit()
    invalidate_cache()
    return {"ok": True}

@app.get("/api/contracts")
def get_contracts():
    if not ADDRESSES_PATH.exists():
        raise HTTPException(500, f"Deployment artifact not found at {ADDRESSES_PATH}")
    try:
        with ADDRESSES_PATH.open() as f:
            contracts = json.load(f)
        contracts["USDC"] = USDC_ADDRESS
        contracts["Forwarder"] = FORWARDER_ADDRESS
        contracts["VSPToken"] = VSP_ADDRESS
        contracts = {k: v.lower() if isinstance(v, str) else v for k, v in contracts.items()}
        print(f"Returning {len(contracts)} contracts from /api/contracts")
        return contracts
    except Exception as e:
        import traceback
        print("ERROR in /api/contracts:", str(e))
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to load contracts: {str(e)}")



@app.get("/api/claim-status/{claim_text}")
def claim_status(claim_text: str, user: str = None, db: Session = Depends(get_db)):
    """Return full claim state including on-chain stakes and verity score.
    Uses strict hash matching only — no fuzzy/similarity resolution."""
    on_chain = compute_one(db, claim_text, top_k=5)
    post_id = on_chain.get("post_id")

    result = {
        "on_chain": on_chain,
        "stake_support": 0,
        "stake_challenge": 0,
        "user_support": 0,
        "user_challenge": 0,
        "verity_score": 0.0,
        "author": "Unknown",
    }

    if post_id is not None:
        try:
            from chain.chain_db import get_stake_totals as db_stakes, get_verity_score as db_vs, get_user_stake as db_user
            support, challenge = db_stakes(db, post_id)
            result["stake_support"] = support
            result["stake_challenge"] = challenge
            result["verity_score"] = db_vs(db, post_id)

            if user:
                result["user_support"] = db_user(db, user, post_id, 0)
                result["user_challenge"] = db_user(db, user, post_id, 1)
        except Exception as e:
            import traceback
            print(f"Failed to read state for post_id={post_id}: {e}")
            print(traceback.format_exc())

    return result


@app.get("/api/claims/{post_id}/user-stake")
def get_user_stake_endpoint(post_id: int, user: str = None, db: Session = Depends(get_db)):
    """Get user's stake on a specific post by post_id."""
    result = {"user_support": 0, "user_challenge": 0}
    if not user:
        return result
    try:
        from chain.chain_db import get_user_stake as db_user
        result["user_support"] = db_user(db, user, post_id, 0)
        result["user_challenge"] = db_user(db, user, post_id, 1)
    except Exception as e:
        print(f"Failed to read user stake for post_id={post_id}, user={user}: {e}")
    return result



@app.post("/api/user-stakes")
def get_user_stakes_batch(body: dict, db: Session = Depends(get_db)):
    """Get user's stake on multiple posts in a single request.
    
    Body: {"user": "0x...", "post_ids": [1, 2, 3, ...]}
    Returns: {"stakes": {"1": {user_support: ..., user_challenge: ...}, ...}}
    """
    user = body.get("user")
    post_ids = body.get("post_ids", [])
    stakes = {}
    if not user or not post_ids:
        return {"stakes": stakes}
    try:
        from chain.chain_db import get_user_stake as db_user
        for pid in post_ids:
            try:
                stakes[str(pid)] = {
                    "user_support": db_user(db, user, pid, 0),
                    "user_challenge": db_user(db, user, pid, 1),
                }
            except Exception:
                stakes[str(pid)] = {"user_support": 0, "user_challenge": 0}
    except Exception as e:
        print(f"Batch user-stakes failed: {e}")
    return {"stakes": stakes}

@app.get("/api/claims/{post_id}/debug")
def debug_claim(post_id: int):
    """Debug: show raw on-chain data for a claim to verify VS calculation."""
    from chain.chain_reader import get_stake_totals, get_verity_score, _get_score_engine
    result = {}
    try:
        support, challenge = get_stake_totals(post_id)
        result["stake_support"] = support
        result["stake_challenge"] = challenge
        result["stake_total"] = support + challenge
        if support + challenge > 0:
            result["simple_vs"] = ((support - challenge) / (support + challenge)) * 100
        else:
            result["simple_vs"] = 0
    except Exception as e:
        result["stake_error"] = str(e)
    try:
        se = _get_score_engine()
        vs_ray = se.functions.effectiveVSRay(post_id).call()
        result["effectiveVSRay_raw"] = str(vs_ray)
        result["effectiveVS_pct"] = (vs_ray / 1e18) * 100
    except Exception as e:
        result["vs_ray_error"] = str(e)
    result["get_verity_score_result"] = get_verity_score(post_id)
    return result


# Old /api/interpret endpoint removed — replaced by /api/article/{topic}
# Old /api/disambiguate endpoint removed — now in article_routes.py


class CreateClaimRequest(BaseModel):
    text: str = Field(..., min_length=3)


@app.post("/api/claims/create")
def create_claim_endpoint(req: CreateClaimRequest):
    # patch_bundle06_moderation_activation: gate the MM-signed direct claim
    # path too (mirrors the relay createClaim gate).
    from moderation import check_content
    _mod = check_content(req.text)
    if getattr(_mod, "unavailable", False):
        raise HTTPException(503, "Moderation temporarily unavailable - please retry shortly.")
    if not _mod.allowed:
        raise HTTPException(400, f"Content blocked: {_mod.reason}")
    try:
        tx_hash = create_claim(req.text)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to create claim: {str(e)}")


class RecordClaimRequest(BaseModel):
    text: str = Field(..., min_length=1)
    post_id: int = Field(..., ge=0)


@app.post("/api/claims/record")
def record_claim_endpoint(req: RecordClaimRequest, db: Session = Depends(get_db)):
    """Record a claim's on-chain post_id in the local DB.
    Called by the frontend after a successful on-chain creation."""
    try:
        db.execute(sql_text(
            "UPDATE claim SET post_id = :pid "
            "WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) AND post_id IS NULL"
        ), {"pid": req.post_id, "t": req.text})
        db.commit()
        return {"ok": True, "post_id": req.post_id}
    except Exception as e:
        print(f"record_claim failed: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/claims/check-onchain")
def check_claim_onchain(text: str, db: Session = Depends(get_db)):
    """Check if a claim already exists on-chain. Returns post_id if it does.
    Also syncs the local DB if a match is found."""
    from chain.check_duplicate import check_claim_exists_onchain
    result = check_claim_exists_onchain(text)
    if result and result.get("post_id") is not None:
        post_id = result["post_id"]
        # Sync local DB
        try:
            db.execute(sql_text(
                "UPDATE claim SET post_id = :pid "
                "WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) AND post_id IS NULL"
            ), {"pid": post_id, "t": text})
            db.execute(sql_text(
                "UPDATE article_sentence SET post_id = :pid "
                "WHERE LOWER(TRIM(text)) = LOWER(TRIM(:t)) AND post_id IS NULL"
            ), {"pid": post_id, "t": text})
            db.commit()
        except Exception as e:
            print(f"check-onchain DB sync failed: {e}")
        return {"exists": True, "post_id": post_id}
    return {"exists": False, "post_id": None}



class StakeRequest(BaseModel):
    claim_id: int = Field(..., ge=0)
    side: str = Field(..., pattern="^(support|challenge)$")
    amount: int = Field(..., gt=0)




# ── Pool price endpoint (Track B public-AMM era) — patch_trackb_pool_reader ──

@app.get("/api/pool/price")
def pool_price():
    """Public pool state for the trade surface. Three-state semantics:
      not configured      -> 200 {"configured": false}  (deliberate dark state;
                             FE renders the legacy MM surface)
      configured, read OK -> 200 {configured, price, reserves, circulating, swap_url}
      configured, FAILED  -> 503 (FE keeps the pool surface and shows
                             'price unavailable' — it must NOT fall back to the
                             MM on a transient RPC failure)
    """
    from config import POOL_PAIR_ADDRESS, SWAP_URL
    if not POOL_PAIR_ADDRESS:
        return {"configured": False}
    from fastapi import HTTPException
    from chain.pool_price import read_pool_state, read_vsp_circulating_v2
    try:
        state = read_pool_state()
    except Exception as e:
        print(f"pool/price read failed: {e}")
        raise HTTPException(503, "pool read failed")
    out = {"configured": True, "swap_url": SWAP_URL, **state}
    try:
        out["vsp_circulating"] = read_vsp_circulating_v2()
    except Exception as e:
        # price is still served; circulating is additive
        print(f"pool/price circulating read failed: {e}")
    return out


# ── Token read endpoints (replaces direct chain reads from frontend) ──────────

@app.get("/api/token/allowance")
def token_allowance(owner: str, spender: str):
    """Read VSP token allowance. Frontend calls this instead of readContract."""
    from chain.provider import w3  # patch_trackb_shared_w3
    from web3 import Web3
    from chain.abi import VSP_TOKEN_ABI
    from config import VSP_TOKEN_ADDRESS
    try:
        token = w3.eth.contract(
            address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
            abi=VSP_TOKEN_ABI,
        )
        val = token.functions.allowance(
            Web3.to_checksum_address(owner),
            Web3.to_checksum_address(spender),
        ).call()
        return {"allowance": str(val)}
    except Exception as e:
        print(f"token/allowance failed: {e}")
        return {"allowance": "0"}


@app.get("/api/token/balance")
def token_balance(address: str):
    """Read VSP token balance. Frontend calls this instead of readContract."""
    from chain.provider import w3  # patch_trackb_shared_w3
    from web3 import Web3
    from chain.abi import VSP_TOKEN_ABI
    from config import VSP_TOKEN_ADDRESS
    try:
        token = w3.eth.contract(
            address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
            abi=VSP_TOKEN_ABI,
        )
        val = token.functions.balanceOf(
            Web3.to_checksum_address(address),
        ).call()
        return {"balance": str(val)}
    except Exception as e:
        print(f"token/balance failed: {e}")
        return {"balance": "0"}




@app.post("/api/reindex/{post_id}")
def reindex_post(post_id: int, request: Request, user: str = None):
    """Trigger immediate reindex of a post, user stakes, and invalidate article cache.
    security review 2026-09 (Low): admin-gated — this burns RPC on demand."""
    require_admin(request, action="reindex", params={"post_id": post_id, "user": user})
    # patch_session_leak_main_reindex: db.close() was after the work, so
    # any exception in index_post/execute/commit would skip the close
    # and leak the session until the postgres idle-tx timeout (5min)
    # reaped it. Use try/finally to guarantee close.
    from chain_indexer import index_post
    from db import get_session_factory
    from sqlalchemy import text as sql_text
    db = get_session_factory()()
    try:
        users = [user] if user else None
        index_post(db, post_id, user_addresses=users)
        db.execute(sql_text(
            "UPDATE topic_article SET cached_response = NULL "
            "WHERE article_id IN ("
            "  SELECT DISTINCT sec.article_id FROM article_section sec "
            "  JOIN article_sentence s ON s.section_id = sec.section_id "
            "  WHERE s.post_id = :pid"
            ")"
        ), {"pid": post_id})
        db.commit()
        return {"ok": True, "post_id": post_id}
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        return {"ok": False, "error": str(e)}
    finally:
        db.close()

@app.post("/api/claims/stake")
def stake_endpoint(req: StakeRequest):
    # patch_bundle06_dms_lockdown_stake: MM-key-signing, no counterparty
    # sig. Disabled in prod (and on Fuji unless ALLOW_DIRECT_MM_SIGNING
    # is set). Prod staking goes through /api/relay/async. See config.py.
    if not DIRECT_MM_SIGNING_ENABLED:
        raise HTTPException(404)
    try:
        tx_hash = stake_claim(req.claim_id, req.side, req.amount)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to stake: {str(e)}")


class WithdrawRequest(BaseModel):
    claim_id: int = Field(..., ge=0)
    side: str = Field(..., pattern="^(support|challenge)$")
    amount: int = Field(..., gt=0)
    lifo: bool = Field(default=True)


@app.post("/api/claims/unstake")
def unstake_endpoint(req: WithdrawRequest):
    # patch_bundle06_dms_lockdown_unstake: MM-key-signing, no counterparty
    # sig. Disabled in prod (and on Fuji unless ALLOW_DIRECT_MM_SIGNING
    # is set). Prod unstaking goes through /api/relay/async. See config.py.
    if not DIRECT_MM_SIGNING_ENABLED:
        raise HTTPException(404)
    try:
        from chain.stake import withdraw_stake
        tx_hash = withdraw_stake(req.claim_id, req.side, req.amount, req.lifo)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to unstake: {str(e)}")


class CreateLinkRequest(BaseModel):
    independent_post_id: int = Field(..., ge=0)
    dependent_post_id: int = Field(..., ge=0)
    is_challenge: bool


@app.post("/api/links/create")
def create_link_endpoint(req: CreateLinkRequest):
    # patch_bundle06_dms_lockdown_link: MM-key-signing, no counterparty
    # sig. Disabled in prod (and on Fuji unless ALLOW_DIRECT_MM_SIGNING
    # is set). Prod link creation goes through /api/relay/async. See config.py.
    if not DIRECT_MM_SIGNING_ENABLED:
        raise HTTPException(404)
    try:
        from chain.claim_registry import create_link
        tx_hash = create_link(req.independent_post_id, req.dependent_post_id, req.is_challenge)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to create link: {str(e)}")