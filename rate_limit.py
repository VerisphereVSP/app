# app/rate_limit.py
"""
Rate limiting and gas budget protection.

Three layers:
  1. Per-IP rate limiting on all endpoints (general anti-spam)
  2. Per-address rate limiting on relay endpoint (gas budget protection)
  3. MM wallet balance check before relay (circuit breaker)
  4. Per-IP rate limiting on AI endpoints (cost protection)

Uses in-memory sliding windows. No external dependencies (no Redis).
Suitable for single-process deployment. For multi-process, switch to Redis.
"""

import os
import time
import logging
from collections import defaultdict
from functools import wraps
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, JSONResponse

logger = logging.getLogger(__name__)

# patch_followup_limiter_logvis: ensure limiter warnings reach docker logs
# regardless of how uvicorn configures logging. The module logger gets its own
# stdout StreamHandler (idempotent) and stops propagating, so a 429 / abuse
# event is always greppable in `docker compose logs` — the observability gap
# that hid the 2026-06-05 shared-bucket 429s for days.
import sys as _vsp_sys
if not any(getattr(_h, "_vsp_logvis", False) for _h in logger.handlers):
    _vsp_h = logging.StreamHandler(_vsp_sys.stdout)
    _vsp_h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _vsp_h._vsp_logvis = True
    logger.addHandler(_vsp_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ── Sliding window counter ─────────────────────────────────

class SlidingWindow:
    """In-memory sliding window rate limiter."""

    def __init__(self):
        # key -> list of timestamps
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._weighted: dict[str, list] = defaultdict(list)  # patch_public_hardening

    def check(self, key: str, max_hits: int, window_seconds: int) -> tuple[bool, int]:
        """Returns (allowed, remaining). Prunes expired entries."""
        now = time.time()
        cutoff = now - window_seconds
        hits = self._hits[key]
        # Prune old entries
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= max_hits:
            return False, 0
        hits.append(now)
        return True, max_hits - len(hits)

    def check_weighted(self, key: str, weight: int, max_units: int, window_seconds: int) -> tuple[bool, int]:
        """Meter UNITS (e.g. sentences), not calls. A single call consumes `weight`
        units. Returns (allowed, remaining_units). patch_public_hardening."""
        now = time.time()
        cutoff = now - window_seconds
        entries = self._weighted[key]
        entries[:] = [(t, w) for (t, w) in entries if t > cutoff]
        used = sum(w for _, w in entries)
        if used + weight > max_units:
            return False, max(0, max_units - used)
        entries.append((now, weight))
        return True, max_units - used - weight

    def cleanup(self, max_age: int = 3600, prefixes: tuple = ()):
        """Remove keys with no recent hits. Call periodically.

        `prefixes` restricts the sweep to keys starting with one of them, so
        high-cardinality short-window buckets can be reaped on a much shorter
        fuse than the daily budgets, which must survive 24h.
        """
        now = time.time()
        cutoff = now - max_age

        def _sweep(d, last_ts):
            dead = [
                k for k, v in d.items()
                if (not prefixes or k.startswith(prefixes))
                and (not v or last_ts(v) < cutoff)
            ]
            for k in dead:
                del d[k]

        _sweep(self._hits, lambda v: v[-1])
        _sweep(self._weighted, lambda v: v[-1][0])


_limiter = SlidingWindow()


# ── Configuration ──────────────────────────────────────────

# General API rate limits (per IP)
# patch_ratelimit_general_env: env-overridable so a single-origin deployment
# (e.g. the dev box, where all browser traffic reaches the app from the one
# Vite-proxy container IP, making this per-IP cap behave as a shared global
# bucket) can raise the ceiling without a code change. Defaults unchanged, so
# prod (real per-client IPs behind nginx) keeps 120/60s.
def _int_env(_name, _default):
    try:
        return int(os.getenv(_name, str(_default)))
    except ValueError:
        return _default
GENERAL_RATE_LIMIT = _int_env("GENERAL_RATE_LIMIT", 120)    # requests per window
GENERAL_RATE_WINDOW = _int_env("GENERAL_RATE_WINDOW", 60)   # seconds

# Per-endpoint per-IP limits (requests per minute), loaded once at startup from
# a JSON file so individual endpoints can be tuned — or effectively disabled
# with a value <= 0 — without a code change. Keys are ROUTE TEMPLATES exactly as
# declared ("/api/claims/{post_id}/live"), not concrete paths, so all ids share
# one bucket. Endpoints not listed get default_per_minute. This caps any single
# endpoint (e.g. an RPC-backed read) independently of the general cap above,
# which only bounds an IP's TOTAL traffic.
ENDPOINT_RATE_WINDOW = 60  # "per minute" by definition
_TEMPLATE_ERR_LOGGED = False  # patch_endpoint_limiter_routematch_isolation


def _load_endpoint_limits() -> tuple[int, dict]:
    """(default_per_minute, {route_template: per_minute}) from RATE_LIMITS_FILE.

    Missing or malformed config degrades to the default for every endpoint —
    a bad ops edit must never keep the API from starting.
    """
    import json
    path = os.getenv("RATE_LIMITS_FILE") or os.path.join(
        os.path.dirname(__file__), "ops", "rate_limits.json"
    )
    default = 100
    per_endpoint: dict = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
        default = int(cfg.get("default_per_minute", default))
        for tpl, lim in (cfg.get("endpoints") or {}).items():
            per_endpoint[str(tpl)] = int(lim)
        logger.info(
            "endpoint rate limits: default=%d/min, %d override(s) (%s)",
            default, len(per_endpoint), path,
        )
    except FileNotFoundError:
        logger.info(
            "endpoint rate limits: no config at %s; default %d/min for all endpoints",
            path, default,
        )
    except Exception as e:
        logger.warning(
            "endpoint rate limits: could not load %s (%s); default %d/min for all endpoints",
            path, e, default,
        )
    return default, per_endpoint


ENDPOINT_RATE_DEFAULT, ENDPOINT_RATE_LIMITS = _load_endpoint_limits()

# Relay rate limits (per user address)
RELAY_RATE_LIMIT = 60           # relay txs per window
RELAY_RATE_WINDOW = 300         # 5 minutes

# AI endpoint rate limits (per IP)
AI_RATE_LIMIT = 10              # AI calls per window
AI_RATE_WINDOW = 300            # 5 minutes

# Gas budget: minimum MM wallet AVAX balance before circuit-breaking relays
MIN_MM_AVAX_WEI = 50_000_000_000_000_000  # 0.05 AVAX — ~20 relay txs at 25 gwei
# How often to re-check balance (don't check every request)
BALANCE_CHECK_INTERVAL = 60     # seconds

# patch_bundle06_xff_trusted_proxy (config): how many trusted reverse-
# proxy hops sit in front of the app. 0 = no proxy (dev) -> ignore
# X-Forwarded-For and use the socket peer. 1 = one nginx in front
# (prod) -> trust exactly the right-most XFF entry. Anything beyond the
# trusted hop count is client-controlled and must never be honored.
try:
    TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
except ValueError:
    TRUSTED_PROXY_HOPS = 0
if TRUSTED_PROXY_HOPS < 0:
    TRUSTED_PROXY_HOPS = 0

# AI cost budget: max AI calls per day (across all users)
AI_DAILY_BUDGET = 500
AI_DAILY_WINDOW = 86400         # 24 hours

# patch_public_hardening: per-SENTENCE (embedding) cost tier — metering embeddings, not
# calls, is the real denial-of-wallet lever for batch endpoints. Env-tunable.
AI_BATCH_SENT_LIMIT = _int_env("VSP_AI_BATCH_SENT_LIMIT", 500)     # sentences per window, per IP
AI_BATCH_SENT_WINDOW = _int_env("VSP_AI_BATCH_SENT_WINDOW", 300)   # seconds
AI_BATCH_SENT_DAILY = _int_env("VSP_AI_BATCH_SENT_DAILY", 20000)   # sentences per day, global

# Named AI budgets: (per-IP calls, window seconds, global calls per day). An
# endpoint that names one is metered against its OWN buckets instead of drawing
# down the shared AI_DAILY_BUDGET. Atomicity fires on every claim-creation
# attempt, so sharing a 500/day pool with article generation would starve one or
# the other.
_AI_BUDGETS = {
    "atomicity": (
        _int_env("VSP_ATOMICITY_RATE_LIMIT", 60),
        _int_env("VSP_ATOMICITY_RATE_WINDOW", 300),
        _int_env("VSP_ATOMICITY_DAILY_BUDGET", 5000),
    ),
}

# The same idea for the per-SENTENCE (ai_batch) tier. /claims/locate needs its
# own buckets rather than match-batch's: it fires continuously as a reader
# scrolls an article (hundreds of sentences per page is normal, where
# match-batch's 500/window was sized for occasional explicit calls), and it
# embeds only the sentences that survive its token prefilter, so the submitted
# count overstates its real cost by a wide margin.
_AI_BATCH_BUDGETS = {
    "locate": (
        _int_env("VSP_LOCATE_SENT_LIMIT", 3000),     # sentences per window, per IP
        _int_env("VSP_LOCATE_SENT_WINDOW", 300),     # seconds
        _int_env("VSP_LOCATE_SENT_DAILY", 500_000),  # sentences per day, global
    ),
}

# patch_postreview_mm_trade_limit: anti-griefing limits for the MM money
# endpoints (/api/mm/buy|sell). These endpoints cannot steal (they need the
# user's own funds + permit) but each accepted trade costs the MM settlement
# gas, so with no per-caller cap they are a gas-drain accelerator (F-4,
# LAUNCH-RISK-AUDIT 2026-07-06 §3.2). Deliberately NOT public_endpoint(): the
# money routes stay same-origin (no permissive CORS) — this is only the
# rate/cost tier. NOTE dev topology: on the dev box all browser traffic
# arrives from the one Vite-proxy container IP, so the per-IP bucket is
# shared there (same caveat as GENERAL_RATE_LIMIT) — the per-address bucket
# still distinguishes users; raise the *_IP limit via env on dev if needed.
MM_TRADE_LIMIT_IP = _int_env("VSP_MM_TRADE_LIMIT_IP", 20)        # trades per window, per IP
MM_TRADE_LIMIT_ADDR = _int_env("VSP_MM_TRADE_LIMIT_ADDR", 10)    # trades per window, per address
MM_TRADE_WINDOW = _int_env("VSP_MM_TRADE_WINDOW", 60)            # seconds
MM_TRADE_DAILY_GLOBAL = _int_env("VSP_MM_TRADE_DAILY_GLOBAL", 5000)  # trades per day, all callers


def check_mm_trade(ip: str, user_address: str) -> None:
    """Per-IP + per-address sliding-window limits and a global daily circuit
    breaker for the MM money endpoints. Call at the TOP of mm_buy/mm_sell,
    before any DB or chain work. Raises HTTPException (429/503) on breach.
    patch_postreview_mm_trade_limit."""
    addr = (user_address or "").strip().lower()
    ok_ip, _ = _limiter.check(f"mmtrade:ip:{ip}", MM_TRADE_LIMIT_IP, MM_TRADE_WINDOW)
    if not ok_ip:
        logger.warning("MM trade rate limit (IP) exceeded for %s", ip)
        raise HTTPException(
            429,
            f"Too many trades from this IP. Limit: {MM_TRADE_LIMIT_IP} per {MM_TRADE_WINDOW}s.",
        )
    ok_addr, _ = _limiter.check(f"mmtrade:addr:{addr}", MM_TRADE_LIMIT_ADDR, MM_TRADE_WINDOW)
    if not ok_addr:
        logger.warning("MM trade rate limit (address) exceeded for %s", addr)
        raise HTTPException(
            429,
            f"Too many trades from this address. Limit: {MM_TRADE_LIMIT_ADDR} per {MM_TRADE_WINDOW}s.",
        )
    ok_g, _ = _limiter.check("mmtrade:global:daily", MM_TRADE_DAILY_GLOBAL, AI_DAILY_WINDOW)
    if not ok_g:
        logger.warning("MM global daily trade budget exhausted")
        raise HTTPException(
            503,
            "MM trading temporarily unavailable — daily trade budget reached. Try again tomorrow.",
        )


# Paths that receive permissive (public) CORS via the app's PublicCORSMiddleware.
# public_endpoint() registers here; the CORS middleware reads it at request time.
PUBLIC_CORS_PATHS: set = set()


# ── Gas budget circuit breaker ─────────────────────────────

_last_balance_check = 0.0
_mm_balance_ok = True


# ── Relayer SPEND-RATE circuit breaker (security follow-up 2026-09-10) ──
# The balance breaker only trips once the wallet is nearly empty; a drain
# of any shape (known or not) burns the whole float first. This breaker
# bounds the RATE: relay-paid gas (and any value) is accounted per rolling
# hour and the relay refuses (503 + alert) when the budget is exceeded.
import os as _os
from collections import deque as _deque
RELAY_SPEND_BUDGET_WEI = int(float(_os.getenv("RELAY_SPEND_BUDGET_AVAX_PER_HOUR", "0.5")) * 1e18)
_spend_window = _deque()  # (timestamp, wei)
_spend_alerted_at = 0.0


def record_relay_spend(wei: int) -> None:
    """Call with the real cost (gasUsed * effectiveGasPrice) of every relay-paid tx."""
    now = time.time()
    _spend_window.append((now, int(wei)))
    while _spend_window and _spend_window[0][0] < now - 3600:
        _spend_window.popleft()


def relay_spend_last_hour() -> int:
    now = time.time()
    while _spend_window and _spend_window[0][0] < now - 3600:
        _spend_window.popleft()
    return sum(w for _, w in _spend_window)


def check_relay_spend_budget() -> bool:
    global _spend_alerted_at
    spent = relay_spend_last_hour()
    if spent <= RELAY_SPEND_BUDGET_WEI:
        return True
    if time.time() - _spend_alerted_at > 600:
        _spend_alerted_at = time.time()
        try:
            import notify
            notify.send_alert("relay_spend_breaker",
                              f"Relay spend {spent / 1e18:.4f} AVAX in the last hour exceeds budget "
                              f"{RELAY_SPEND_BUDGET_WEI / 1e18:.4f} AVAX — relay paused (503). Investigate before raising RELAY_SPEND_BUDGET_AVAX_PER_HOUR.")
        except Exception:
            logger.exception("relay spend breaker: alert failed")
    logger.error("relay spend breaker tripped: %.4f AVAX/h > budget", spent / 1e18)
    return False


def check_mm_balance() -> bool:
    """Check if the MM wallet has enough AVAX to relay.
    Cached for BALANCE_CHECK_INTERVAL seconds."""
    global _last_balance_check, _mm_balance_ok

    now = time.time()
    if now - _last_balance_check < BALANCE_CHECK_INTERVAL:
        return _mm_balance_ok

    try:
        from mm_wallet import w3
        from config import MM_ADDRESS
        from web3 import Web3

        # patch_postreview_relay_gas_breaker: since the relay-key separation
        # (#298, 2026-06-09) the RELAY wallet pays meta-tx gas, not the MM —
        # but this breaker still watched MM_ADDRESS, so it could neither trip
        # when the relay ran dry nor stay quiet when only the MM was low.
        # Watch the wallet that actually pays; fall back to MM_ADDRESS only
        # if RELAY_ADDRESS is unset (pre-separation behavior).
        _gas_payer = os.getenv("RELAY_ADDRESS", "").strip() or MM_ADDRESS
        balance = w3.eth.get_balance(Web3.to_checksum_address(_gas_payer))
        _mm_balance_ok = balance >= MIN_MM_AVAX_WEI
        _last_balance_check = now

        if not _mm_balance_ok:
            logger.warning(
                "Relay gas wallet (%s) AVAX balance critically low: %s wei (min: %s). "
                "Relay is paused.",
                _gas_payer, balance, MIN_MM_AVAX_WEI,
            )
        return _mm_balance_ok
    except Exception as e:
        logger.warning("Failed to check MM balance: %s", e)
        # Fail open — don't block relay if we can't check
        _last_balance_check = now
        return True


# ── Helper to extract client IP ────────────────────────────

def _client_ip(request: Request) -> str:
    # patch_bundle06_xff_trusted_proxy: X-Forwarded-For is attacker-
    # controlled. Honor only TRUSTED_PROXY_HOPS proxy hops at the RIGHT
    # end of the chain.
    #   hops == 0 (dev, no proxy): ignore XFF entirely, use socket peer.
    #   hops == 1 (prod behind one nginx): use the right-most XFF entry,
    #   the client IP our own trusted proxy observed and appended.
    peer = request.client.host if request.client else "unknown"
    if TRUSTED_PROXY_HOPS <= 0:
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    chain = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not chain:
        return peer
    idx = len(chain) - TRUSTED_PROXY_HOPS
    if idx < 0:
        # Chain shorter than the configured trusted-hop count (misconfig
        # or a spoof attempt): fall back to the socket peer, which a
        # client cannot forge via request headers.
        return peer
    return chain[idx]


# ── FastAPI middleware ─────────────────────────────────────

def _route_template(request: Request) -> Optional[str]:
    """The matched route's path template, read from the ASGI scope.

    patch_endpoint_limiter_scope_route (2026-08-19): walking app.routes is
    unreliable here -- routers included with a prefix appear as pathless
    _IncludedRouter entries that match FULL before the concrete APIRoute is
    reached, and their children are not exposed on the entry. Starlette
    writes the matched route into scope["route"] during routing, so we read
    that instead. Available only AFTER call_next, hence metering is
    post-dispatch (the tripping request is counted, the next is blocked).
    None means genuinely unrouted (404) -- deliberately unmetered.
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) if route is not None else None

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting: a general cap on total traffic, plus an
    independent per-endpoint cap (config-driven, see _load_endpoint_limits)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip health checks
        if request.url.path in ("/healthz", "/docs", "/openapi.json"):
            return await call_next(request)

        ip = _client_ip(request)
        key = f"general:{ip}"
        allowed, remaining = _limiter.check(key, GENERAL_RATE_LIMIT, GENERAL_RATE_WINDOW)

        if not allowed:
            logger.warning("Rate limit exceeded for IP %s on %s", ip, request.url.path)
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(GENERAL_RATE_WINDOW)},
            )

        response = await call_next(request)

        # patch_endpoint_limiter_scope_route: scope["route"] is only populated
        # once routing has happened, so the per-endpoint cap is metered here.
        template = _route_template(request)
        if template is not None:
            limit = ENDPOINT_RATE_LIMITS.get(template, ENDPOINT_RATE_DEFAULT)
            if limit > 0:
                ok, _ = _limiter.check(f"ep:{template}:{ip}", limit, ENDPOINT_RATE_WINDOW)
                if not ok:
                    logger.warning(
                        "Endpoint rate limit exceeded for IP %s on %s (%d/min)",
                        ip, template, limit,
                    )
                    return JSONResponse(
                        status_code=429,
                        content={"detail": f"Too many requests to this endpoint. Limit: {limit} per minute."},
                        headers={"Retry-After": str(ENDPOINT_RATE_WINDOW)},
                    )

        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


# ── Decorators for specific endpoints ─────────────────────

def relay_rate_limit(func):
    """Decorator for the relay endpoint. Per-address + gas budget check."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Extract request from kwargs or args
        body = kwargs.get("body")
        request = kwargs.get("request")

        # F-1 (private disclosure 2026-09): the per-address bucket resets with a
        # fresh EOA, so add a per-IP bucket the attacker cannot reset for free.
        _req = kwargs.get("request") or next((a for a in args if hasattr(a, "client")), None)
        _ip = getattr(getattr(_req, "client", None), "host", None) or "unknown"
        _ok, _ = _limiter.check(f"relay-ip:{_ip}", RELAY_RATE_LIMIT, RELAY_RATE_WINDOW)
        if not _ok:
            logger.warning("Relay rate limit exceeded for IP %s", _ip)
            raise HTTPException(429, "Too many relay requests from this network address.")
        # Per-address rate limit
        if body and hasattr(body, "request") and hasattr(body.request, "from_"):
            addr = body.request.from_.lower()
            key = f"relay:{addr}"
            allowed, remaining = _limiter.check(key, RELAY_RATE_LIMIT, RELAY_RATE_WINDOW)
            if not allowed:
                logger.warning("Relay rate limit exceeded for address %s", addr)
                raise HTTPException(
                    429,
                    f"Too many relay requests from this address. "
                    f"Limit: {RELAY_RATE_LIMIT} per {RELAY_RATE_WINDOW}s.",
                )

        # Spend-rate circuit breaker (bounds any drain shape by construction)
        if not check_relay_spend_budget():
            raise HTTPException(503, "Relay temporarily paused — hourly spend budget exceeded; operator alerted.")
        # Gas budget circuit breaker
        if not check_mm_balance():
            raise HTTPException(
                503,
                "Relay temporarily unavailable — gas budget depleted. "
                "Please try again later or use a direct wallet transaction.",
            )

        return await func(*args, **kwargs)

    return wrapper


def ai_rate_limit(func):
    """Decorator for AI-calling endpoints. Per-IP + daily global budget."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        # Try to get request from FastAPI dependency injection
        request = kwargs.get("request")
        ip = "unknown"
        if request:
            ip = _client_ip(request)
        elif args:
            # Check if any arg looks like a Request
            for arg in args:
                if isinstance(arg, Request):
                    ip = _client_ip(arg)
                    break

        # Per-IP limit
        key = f"ai:{ip}"
        allowed, remaining = _limiter.check(key, AI_RATE_LIMIT, AI_RATE_WINDOW)
        if not allowed:
            logger.warning("AI rate limit exceeded for IP %s", ip)
            raise HTTPException(
                429,
                f"Too many AI requests. Limit: {AI_RATE_LIMIT} per {AI_RATE_WINDOW // 60} minutes.",
            )

        # Global daily budget
        key_global = "ai:global:daily"
        allowed_g, _ = _limiter.check(key_global, AI_DAILY_BUDGET, AI_DAILY_WINDOW)
        if not allowed_g:
            logger.warning("Global AI daily budget exhausted")
            raise HTTPException(
                503,
                "AI generation temporarily unavailable — daily budget reached. "
                "Try again tomorrow.",
            )

        return func(*args, **kwargs)

    return wrapper


# ── Periodic cleanup (call from a background task) ─────────

def public_endpoint(path: str, cost_tier: str = "cheap", budget: str | None = None):
    """Mark an endpoint publicly consumable: register it for permissive CORS and apply
    the anti-griefing rate/cost tier. patch_public_hardening.

    cost_tier:
      "ai_batch" — per-IP + global daily SENTENCE budget (weight = len(body['sentences']))
      "ai"       — per-IP + global daily CALL budget (like ai_rate_limit)
      "cheap"    — general per-IP middleware limit only

    budget: name of an entry in _AI_BUDGETS ("ai" tier) or _AI_BATCH_BUDGETS
    ("ai_batch" tier). Meters this endpoint against its own buckets rather than
    the shared ones.

    Router decorator goes ON TOP so FastAPI registers the wrapped function:
        @router.post("/x")
        @public_endpoint("/api/.../x", cost_tier="ai_batch")
        def handler(...): ...
    """
    PUBLIC_CORS_PATHS.add(path)

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                for a in args:
                    if isinstance(a, Request):
                        request = a
                        break
            ip = _client_ip(request) if request is not None else "unknown"

            if cost_tier == "ai_batch":
                body = kwargs.get("body")
                n = 1
                if isinstance(body, dict) and isinstance(body.get("sentences"), list):
                    n = max(1, len(body["sentences"]))
                bucket = budget if budget in _AI_BATCH_BUDGETS else "aibatch"
                per_ip, window, daily = _AI_BATCH_BUDGETS.get(
                    bucket, (AI_BATCH_SENT_LIMIT, AI_BATCH_SENT_WINDOW, AI_BATCH_SENT_DAILY),
                )
                ok_ip, _ = _limiter.check_weighted(f"{bucket}:{ip}", n, per_ip, window)
                if not ok_ip:
                    logger.warning("ai_batch per-IP sentence budget exceeded for %s (n=%d, bucket=%s)", ip, n, bucket)
                    raise HTTPException(429, f"Too many sentences. Limit: {per_ip} per {window}s.")
                ok_g, _ = _limiter.check_weighted(f"{bucket}:global:daily", n, daily, AI_DAILY_WINDOW)
                if not ok_g:
                    logger.warning("ai_batch global daily sentence budget exhausted (bucket=%s)", bucket)
                    raise HTTPException(503, "Batch match temporarily unavailable — daily budget reached.")
            elif cost_tier == "ai":
                bucket = budget if budget in _AI_BUDGETS else "ai"
                per_ip, window, daily = _AI_BUDGETS.get(
                    bucket, (AI_RATE_LIMIT, AI_RATE_WINDOW, AI_DAILY_BUDGET),
                )
                ok_ip, _ = _limiter.check(f"{bucket}:{ip}", per_ip, window)
                if not ok_ip:
                    raise HTTPException(429, f"Too many AI requests. Limit: {per_ip} per {window // 60} minutes.")
                ok_g, _ = _limiter.check(f"{bucket}:global:daily", daily, AI_DAILY_WINDOW)
                if not ok_g:
                    raise HTTPException(503, "AI temporarily unavailable — daily budget reached.")
            # "cheap": general middleware limit already applies

            return func(*args, **kwargs)
        return wrapper
    return decorator


def cleanup_rate_limiter():
    """Remove stale entries. Call every ~10 minutes from a background task."""
    # The per-IP and per-IP-per-endpoint buckets are the high-cardinality
    # classes (one key per (ip, route) pair seen), and they are provably dead
    # two windows after the IP goes quiet — reap them fast rather than letting
    # them ride the 2-day retention the daily AI budgets need.
    _limiter.cleanup(
        max_age=max(GENERAL_RATE_WINDOW, ENDPOINT_RATE_WINDOW) * 2,
        prefixes=("general:", "ep:"),
    )
    _limiter.cleanup(max_age=max(GENERAL_RATE_WINDOW, RELAY_RATE_WINDOW, AI_DAILY_WINDOW) * 2)
