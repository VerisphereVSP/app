# app/config.py
import os
from pathlib import Path
import json

# Network configuration
CHAIN_ID = int(os.getenv("CHAIN_ID", "43113"))
RPC_URL_READ = os.getenv("RPC_URL_READ", os.getenv("RPC_URL", "https://api.avax-test.network/ext/bc/C/rpc"))
RPC_URL = os.getenv("RPC_URL", "")
# patch_bundle10c_backend_hardening_config: an empty RPC_URL on mainnet is fatal — every
# chain-reading code path silently fails or returns wrong data.
# Deferred to after NETWORK is computed (see end of file).

# Determine network name
if CHAIN_ID == 43113:
    NETWORK = "fuji"
elif CHAIN_ID == 43114:
    NETWORK = "mainnet"
else:
    NETWORK = f"chain-{CHAIN_ID}"

# patch_bundle10_rpc_failover: ordered RPC endpoint lists (primary + network public
# fallback). build_w3() fails over across these on transport errors. A single-element
# list behaves exactly as before. An explicit comma-separated override (RPC_URLS /
# RPC_URLS_READ) wins over the derived [primary, public] default.
_PUBLIC_RPC = {
    "fuji": "https://api.avax-test.network/ext/bc/C/rpc",
    "mainnet": "https://api.avax.network/ext/bc/C/rpc",
}


def _rpc_failover_list(override_env, primary, *extra_fallbacks):
    _raw = os.getenv(override_env, "")
    if _raw.strip():
        return [u.strip() for u in _raw.split(",") if u.strip()]
    _cands = [primary] + list(extra_fallbacks)
    _pub = _PUBLIC_RPC.get(NETWORK)
    if _pub:
        _cands.append(_pub)
    # dedup preserving order, drop empties
    return [u for u in dict.fromkeys(_cands) if u]


# Reads prefer their own endpoint (RPC_URL_READ -- often the public endpoint, to keep
# high-volume reads off paid-provider quota) but fall back to the signing endpoint
# (RPC_URL) and then the network public endpoint, so a read-endpoint outage still has
# somewhere to go. On Fuji this yields RPC_READ_URLS = [public, Alchemy].
RPC_WRITE_URLS = _rpc_failover_list("RPC_URLS", RPC_URL)
RPC_READ_URLS = _rpc_failover_list("RPC_URLS_READ", RPC_URL_READ, RPC_URL)

# patch_bundle06_direct_mm_signing_lockdown: the direct MM-key-signing
# endpoints (/api/claims/stake, /api/claims/unstake, /api/links/create)
# sign on-chain txs with the MM hot wallet and carry NO counterparty
# signature. They were only ever used by the retired test-vs.sh /
# test-e2e.sh; prod create/stake/link go through /api/relay/async
# (user-signed). Disabled in prod unconditionally; on non-mainnet they
# stay disabled unless explicitly re-enabled for ad-hoc testnet use.
DIRECT_MM_SIGNING_ENABLED = (
    NETWORK != "mainnet"
    and os.getenv("ALLOW_DIRECT_MM_SIGNING", "0").strip().lower()
    in ("1", "true", "yes", "on")
)

# patch_followup_service_token: shared-secret gate for the MM money endpoints
# (/api/mm/buy,/sell,/transfer,/execute-permit). OFF by default (token unset) so
# dev/Fuji are unaffected. In prod set SERVICE_API_TOKEN in
# env/secrets.<network>.enc.yaml (SOPS); Caddy injects it for browser traffic and
# the batch tool sends it. When set, the four endpoints require a matching
# X-Service-Token header.
SERVICE_API_TOKEN = os.getenv("SERVICE_API_TOKEN", "").strip()
REQUIRE_SERVICE_TOKEN = bool(SERVICE_API_TOKEN)

# patch_bundle10c_backend_hardening_config: fail-loud on missing required env when on mainnet.
# Fuji keeps the convenience fallbacks; mainnet raises RuntimeError on any
# unset value, so a stale /dev/shm/vsp-resolved.env or a typo can never
# silently route mainnet traffic through a Fuji address or empty RPC.
def _require_for_mainnet(name: str, value, fallback_label: str):
    if NETWORK == "mainnet" and (value is None or value == "" or value == fallback_label):
        raise RuntimeError(
            f"config.py: required env var {name} is unset on mainnet "
            f"(fallback {fallback_label!r} is not acceptable for mainnet); "
            f"check /dev/shm/vsp-resolved.env and the resolver pipeline."
        )
    return value

# Load deployed contract addresses
DEPLOYMENTS_DIR = Path(__file__).parent / "deployments"
ADDRESSES_FILE = DEPLOYMENTS_DIR / f"{NETWORK}.json"

if ADDRESSES_FILE.exists():
    with open(ADDRESSES_FILE) as f:
        DEPLOYED = json.load(f)
else:
    print(f"Warning: No deployment file at {ADDRESSES_FILE}")
    DEPLOYED = {}

# Contract addresses
AUTHORITY_ADDRESS = DEPLOYED.get("Authority", "")
VSP_TOKEN_ADDRESS = DEPLOYED.get("VSPToken", "")
POST_REGISTRY_ADDRESS = DEPLOYED.get("PostRegistry", "")
LINK_GRAPH_ADDRESS = DEPLOYED.get("LinkGraph", "")
STAKE_ENGINE_ADDRESS = DEPLOYED.get("StakeEngine", "")
SCORE_ENGINE_ADDRESS = DEPLOYED.get("ScoreEngine", "")
PROTOCOL_VIEWS_ADDRESS = DEPLOYED.get("ProtocolViews", "")
# Forwarder is deployed separately from core (see app/contracts/)
# Its address is either in the core deployment JSON (legacy) or in app/deployments/forwarder.json
FORWARDER_ADDRESS = DEPLOYED.get("Forwarder", "")
if not FORWARDER_ADDRESS:
    import json as _json
    _fwd_path = Path(__file__).parent / "deployments" / "forwarder.json"
    if _fwd_path.exists():
        FORWARDER_ADDRESS = _json.loads(_fwd_path.read_text()).get("Forwarder", "")


# External tokens
USDC_ADDRESS = os.getenv("USDC_ADDRESS", "0x5425890298aed601595a70ab815c96711a31bc65")

# ── patch_trackb_pool_reader: public-AMM era ──
# POOL_PAIR_ADDRESS empty = pool era DARK (endpoint answers {"configured": false},
# FE shows the legacy MM surface). Set it to the SeedPool-deployed MockCPAMM on
# Fuji (runbook) / the real venue pair on mainnet.
POOL_PAIR_ADDRESS = os.getenv("POOL_PAIR_ADDRESS", "").strip()
# Where the FE "Swap" button points (venue swap page). Served by the backend so
# changing venue never needs an FE rebuild.
SWAP_URL = os.getenv("SWAP_URL", "").strip()
# ── patch_venue: venue class for the public pool ──
# mockcpamm (default) = the Fuji rehearsal mock (reserve0()/reserve1()).
# univ2 = canonical UniswapV2 interface (getReserves()): Joe V1 on Fuji,
# Uniswap v2 on Avalanche mainnet (checklist #4). VENUE_ROUTER is served to
# the FE via /api/pool/price so in-app swaps approve + call the router.
POOL_VENUE = os.getenv("POOL_VENUE", "mockcpamm").strip().lower()
VENUE_ROUTER = os.getenv("VENUE_ROUTER", "").strip()
# ── patch_mm_410: Phase 4 retirement flag ──
# true (default) = MM era: routes live, reconcile runs, MM metrics sampled.
# false = pool era: /api/mm/* answers 410 with a pool pointer; the reconcile
# loop and the MM-model metrics (floor/sell/buy) stop. Flipped in
# env/network.fuji.env at Phase 4 — this code is inert until then.
MM_ROUTES_ENABLED = os.getenv("MM_ROUTES_ENABLED", "true").strip().lower() not in ("0", "false", "no")
# security review 2026-09 (Informational): MM buy/sell/execute-permit are fully
# open when SERVICE_API_TOKEN is unset. Fine on a testnet trade surface; on
# mainnet that is a fail-open config, so refuse to start.
if MM_ROUTES_ENABLED and CHAIN_ID == 43114 and not os.getenv("SERVICE_API_TOKEN", "").strip():
    raise RuntimeError("MM_ROUTES_ENABLED on mainnet requires SERVICE_API_TOKEN (fail-closed)")
VSP_ADDRESS = VSP_TOKEN_ADDRESS

# Market maker wallet (reserves — backs outstanding VSP)
_MM_ADDRESS_FUJI_FALLBACK = "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a"
MM_ADDRESS = _require_for_mainnet(
    "MM_ADDRESS",
    os.getenv("MM_ADDRESS", _MM_ADDRESS_FUJI_FALLBACK),
    _MM_ADDRESS_FUJI_FALLBACK,
)

# Treasury wallet (revenue — receives trade fees + relay fees)
# patch_bundle10c_backend_hardening_config: explicit TREASURY_ADDRESS required on mainnet.
# Silently routing fees back to MM in a misconfigured mainnet deploy is
# the exact footgun this guard exists to prevent.
_TREASURY_RAW = os.getenv("TREASURY_ADDRESS", "")
if _TREASURY_RAW:
    TREASURY_ADDRESS = _TREASURY_RAW
else:
    if NETWORK == "mainnet":
        raise RuntimeError(
            "config.py: TREASURY_ADDRESS must be set explicitly on mainnet "
            "(implicit fallback to MM_ADDRESS would silently route fees back to MM)."
        )
    TREASURY_ADDRESS = MM_ADDRESS  # Fuji-only convenience

# patch_trackb_pool_reader: post-MM circulating = totalSupply − these balances.
# CSV env override; default = company-controlled addresses known today
# (MM inventory while it exists + treasury). Deduped, order-stable.
_circ_raw = os.getenv("CIRCULATING_EXCLUDE_ADDRESSES", "").strip()
if _circ_raw:
    CIRCULATING_EXCLUDE_ADDRESSES = [a.strip() for a in _circ_raw.split(",") if a.strip()]
else:
    CIRCULATING_EXCLUDE_ADDRESSES = []
    for _a in (MM_ADDRESS, TREASURY_ADDRESS):
        if _a and _a not in CIRCULATING_EXCLUDE_ADDRESSES:
            CIRCULATING_EXCLUDE_ADDRESSES.append(_a)

# patch_bundle11_cold_reserve: dedicated cold-custody sweep destination,
# segregated from the fee sink (TREASURY_ADDRESS). Swept USDC stays
# floor-backing, so this address is auto-folded into COLD_SAFE_ADDRESSES
# below. Default empty -> no sweep (worker skips). Must differ from MM
# and the fee sink; mismatches fail-fast here.
COLD_RESERVE_ADDRESS = os.getenv("VSP_COLD_RESERVE_ADDRESS", "").strip()
if COLD_RESERVE_ADDRESS:
    _crl = COLD_RESERVE_ADDRESS.lower()
    if not (COLD_RESERVE_ADDRESS.startswith("0x") and len(COLD_RESERVE_ADDRESS) == 42):
        raise RuntimeError(f"config.py: VSP_COLD_RESERVE_ADDRESS malformed: {COLD_RESERVE_ADDRESS!r}")
    if _crl == MM_ADDRESS.lower():
        raise RuntimeError("config.py: VSP_COLD_RESERVE_ADDRESS must differ from MM_ADDRESS (sweep would be a self-send).")
    if _crl == TREASURY_ADDRESS.lower():
        raise RuntimeError("config.py: VSP_COLD_RESERVE_ADDRESS must differ from TREASURY_ADDRESS (cold reserves must be segregated from the fee sink).")


# patch06: virtual reserves — cold-storage addresses whose USDC
# balances are summed into read_usdc_reserves(). Comma-separated env
# var, whitespace-tolerant. Empty/unset → behaves as before
# (balanceOf(MM) only). Each entry must be a valid 0x-prefixed
# address; malformed entries fail-fast at startup.
def _parse_cold_safe_addresses(raw: str) -> list:
    if not raw or not raw.strip():
        return []
    result = []
    seen = set()
    for tok in raw.split(","):
        addr = tok.strip()
        if not addr:
            continue
        # Validate shape: 0x + 40 hex chars
        if not (addr.startswith("0x") and len(addr) == 42 and
                all(c in "0123456789abcdefABCDEF" for c in addr[2:])):
            raise ValueError(
                f"COLD_SAFE_ADDRESSES contains malformed address: {addr!r}"
            )
        # Normalize to lowercase for dedup (web3 will checksum at use site)
        addr_lower = addr.lower()
        # Skip zero address
        if addr_lower == "0x" + "0" * 40:
            continue
        # Skip duplicates (including MM itself — would double-count)
        if addr_lower == MM_ADDRESS.lower() or addr_lower in seen:
            continue
        seen.add(addr_lower)
        result.append(addr)
    return result

COLD_SAFE_ADDRESSES = _parse_cold_safe_addresses(
    os.getenv("COLD_SAFE_ADDRESSES", "")
)

# patch_bundle11_cold_reserve_foldin: the cold-custody sweep destination
# is always counted as floor-backing reserve. Fold it in if not listed.
if COLD_RESERVE_ADDRESS and COLD_RESERVE_ADDRESS.lower() not in {
    _a.lower() for _a in COLD_SAFE_ADDRESSES
}:
    COLD_SAFE_ADDRESSES.append(COLD_RESERVE_ADDRESS)


# Database
DB_USER = os.getenv("DB_USER", "verisphere")
DB_PASS = os.getenv("DB_PASS", "")
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "verisphere")
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# patch_bundle11_remove_dead_openai_model: removed the dead OPENAI_MODEL constant
# (nothing imported config.OPENAI_MODEL). The live LLM selection is LLM_PROVIDER /
# LLM_MODEL, read in llm_provider.py and set to anthropic / claude-haiku-4-5-* in
# env/common.env. OPENAI_API_KEY is kept: OpenAI is still the embeddings provider
# (env/common.env: EMBEDDINGS_PROVIDER=openai).

# Embeddings
EMBEDDINGS_PROVIDER = os.getenv("EMBEDDINGS_PROVIDER", "openai")
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")

# Semantic search thresholds
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.95"))
NEAR_DUPLICATE_THRESHOLD = float(os.getenv("NEAR_DUPLICATE_THRESHOLD", "0.85"))

# Print config (APP-08: password redacted)  # patch_bundle04_5_p22_config_stderr
# Banner goes to stderr so `docker compose logs` still shows it
# (captures both streams) but one-liner pipelines that consume
# `docker compose exec app python -c '...'` stdout aren't polluted.
import sys as _sys_for_banner
_db_url_safe = f"postgresql://{DB_USER}:***@{DB_HOST}:{DB_PORT}/{DB_NAME}"
print("Config loaded:",                              file=_sys_for_banner.stderr)
print(f"  CHAIN_ID: {CHAIN_ID}",                     file=_sys_for_banner.stderr)
print(f"  NETWORK: {NETWORK}",                       file=_sys_for_banner.stderr)
print(f"  POST_REGISTRY: {POST_REGISTRY_ADDRESS}",   file=_sys_for_banner.stderr)
print(f"  FORWARDER: {FORWARDER_ADDRESS}",           file=_sys_for_banner.stderr)
print(f"  MM_ADDRESS: {MM_ADDRESS}",                 file=_sys_for_banner.stderr)
print(f"  RPC_URL: {RPC_URL[:50]}...",               file=_sys_for_banner.stderr)
print(f"  DB: {_db_url_safe}",                       file=_sys_for_banner.stderr)


# Relay fee configuration
RELAY_FEE_MARGIN_PCT = float(os.getenv("RELAY_FEE_MARGIN_PCT", "0.30"))  # 30% margin on gas cost
RELAY_FEE_MIN_VSP = float(os.getenv("RELAY_FEE_MIN_VSP", "0.1"))  # Minimum relay fee
RELAY_FEE_TXN_PCT = float(os.getenv("RELAY_FEE_TXN_PCT", "0.01"))  # 1% of txn value
AVAX_PRICE_USD = float(os.getenv("AVAX_PRICE_USD", "20.0"))  # Fallback AVAX price

# patch_bundle10_5_part2b_treasury_worker: base URL the treasury worker uses to reach the app's
# read API (e.g. /api/mm/floor). Inside the docker network the worker
# reaches the app by service name; override via env if needed.
APP_API_BASE = os.getenv("APP_API_BASE", "http://app:8070")

# patch_bundle10_5_part2b_treasury_worker: treasury worker tunables are read directly from the
# environment inside treasury_worker.py (so switches can be re-read each
# loop iteration). They are intentionally NOT bound here as module
# constants. The only worker value imported from config is APP_API_BASE
# above (plus the existing VSP_TOKEN_ADDRESS / USDC_ADDRESS / MM_ADDRESS
# / TREASURY_ADDRESS).

# patch_bundle10c_backend_hardening_config_eof: late-stage RPC_URL fail-loud on mainnet.
# (Placed at end of file so NETWORK is already defined.)
if NETWORK == "mainnet" and not RPC_URL:
    raise RuntimeError(
        "config.py: RPC_URL is empty on mainnet. Refusing to start; "
        "every chain-reading code path would silently fail."
    )
