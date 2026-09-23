# app/chain/chain_reader.py
"""
On-chain read helpers.

StakeEngine v2 view functions (getUserStake, getPostTotals) already
project epoch gains/losses lazily — they always return the current
virtual balance, not stale snapshots. No separate projection needed.
"""

import json
import logging
from pathlib import Path
from web3 import Web3
from config import STAKE_ENGINE_ADDRESS, SCORE_ENGINE_ADDRESS, RPC_URL, RPC_READ_URLS
from tx_signer import build_w3  # patch_bundle10_rpc_failover_p2

# Lazy-loaded
_stake_rate_policy = None

logger = logging.getLogger(__name__)

import time

# Simple in-memory TTL cache for on-chain reads
_cache: dict = {}
_CACHE_TTL = 30  # seconds

def _cached(key: str, fn, ttl: int = _CACHE_TTL):
    """Return cached value if fresh, otherwise call fn() and cache result."""
    now = time.time()
    if key in _cache:
        val, ts = _cache[key]
        if now - ts < ttl:
            return val
    val = fn()
    _cache[key] = (val, now)
    return val

def clear_cache():
    """Clear all cached chain reads."""
    _cache.clear()



_w3 = None
_stake_engine = None
_score_engine = None


def _load_abi(name):
    # Resolves in both Docker (/core mount) and bare local runs.
    from .abi import load_abi_optional
    return load_abi_optional(name)


STAKE_ENGINE_ABI = _load_abi("StakeEngine") or [
    {
        "type": "function",
        "name": "getPostTotals",
        "inputs": [{"name": "postId", "type": "uint256"}],
        "outputs": [
            {"name": "support", "type": "uint256"},
            {"name": "challenge", "type": "uint256"},
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getUserStake",
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "postId", "type": "uint256"},
            {"name": "side", "type": "uint8"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "sMax",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "protocolPolicy",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "numTranches",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getUserLotInfo",
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "postId", "type": "uint256"},
            {"name": "side", "type": "uint8"},
        ],
        "outputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "weightedPosition", "type": "uint256"},
            {"name": "entryEpoch", "type": "uint256"},
            {"name": "sideTotal", "type": "uint256"},
            {"name": "positionWeight", "type": "uint256"},
        ],
        "stateMutability": "view",
    },
]

SCORE_ENGINE_ABI = _load_abi("ScoreEngine") or [
    {
        "type": "function",
        "name": "effectiveVSRay",
        "inputs": [{"name": "postId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "int256"}],
        "stateMutability": "view",
    },
]


def _get_w3():
    global _w3
    if _w3 is None:
        _w3 = build_w3(RPC_READ_URLS, require_connected=False)
    return _w3


def _get_stake_engine():
    global _stake_engine
    if _stake_engine is None:
        w3 = _get_w3()
        _stake_engine = w3.eth.contract(
            address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
            abi=STAKE_ENGINE_ABI,
        )
    return _stake_engine


def _get_score_engine():
    global _score_engine
    if _score_engine is None:
        w3 = _get_w3()
        _score_engine = w3.eth.contract(
            address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS),
            abi=SCORE_ENGINE_ABI,
        )
    return _score_engine


def get_stake_totals(post_id):
    """Returns (support, challenge) in VSP units. Already projected."""
    def _read():
        se = _get_stake_engine()
        support_wei, challenge_wei = se.functions.getPostTotals(post_id).call()
        return support_wei / 1e18, challenge_wei / 1e18
    try:
        return _cached(f"stakes:{post_id}", _read)
    except Exception as e:
        logger.warning("Failed to read stake totals for post %d: %s", post_id, e)
        return 0.0, 0.0


def get_user_stake(user_address, post_id, side):
    """Returns user's projected stake in VSP units. side: 0=support, 1=challenge."""
    try:
        se = _get_stake_engine()
        addr = Web3.to_checksum_address(user_address)
        amount_wei = se.functions.getUserStake(addr, post_id, side).call()
        return amount_wei / 1e18
    except Exception as e:
        logger.warning(
            "Failed to read user stake for %s post %d side %d: %s",
            user_address, post_id, side, e,
        )
        return 0.0


def get_verity_score(post_id):
    """Effective VS from ScoreEngine.effectiveVSRay, as a float in [-100, 100].

    patch_vs_single_source: there is NO fallback formula. The old stake-share
    fallback silently dropped all link evidence whenever the RPC hiccupped.
    A read failure raises; callers that can tolerate it catch it themselves.
    """
    from chain.vs import read_effective_vs_pct

    def _read_vs():
        return read_effective_vs_pct(_get_score_engine(), post_id)
    return _cached(f"vs:{post_id}", _read_vs)




def _get_rate_bounds():
    """Read rMin and rMax from ProtocolPolicy on-chain. Returns (rMin, rMax) as fractions (0-1)."""
    def _read():
        try:
            se = _get_stake_engine()
            # StakeEngine has a protocolPolicy() getter (Patch 17+)
            rate_policy_addr = se.functions.protocolPolicy().call()
            rate_abi = [
                {"type": "function", "name": "stakeIntRateMinRay",
                 "inputs": [], "outputs": [{"type": "uint256"}], "stateMutability": "view"},
                {"type": "function", "name": "stakeIntRateMaxRay",
                 "inputs": [], "outputs": [{"type": "uint256"}], "stateMutability": "view"},
            ]
            from web3 import Web3
            w3 = _get_w3()
            rp = w3.eth.contract(address=Web3.to_checksum_address(rate_policy_addr), abi=rate_abi)
            r_min = rp.functions.stakeIntRateMinRay().call() / 1e18
            r_max = rp.functions.stakeIntRateMaxRay().call() / 1e18
            return (r_min, r_max)
        except Exception as e:
            logger.warning("Failed to read rate bounds: %s", e)
            return (0.01, 1.00)  # fallback
    return _cached("rate_bounds", _read, ttl=300)  # cache 5 min

def get_estimated_apr(post_id, side="support", user_address=None):
    """Estimate annualized rate for a position on this post.

    Formula (from whitepaper §3.2 + StakeEngine.sol):
      r_base   = rMin + (rMax - rMin) * v * participation
      r_lot    = r_base * positionWeight     (per-lot, independent)
    where:
      v             = abs(VS) / 100             (truth pressure, 0-1)
      participation = T / sMax                  (post size factor, 0-1)
      positionWeight∈ [0, 1], lot midpoint weight (sole staker: 0.5)
      rMin, rMax    annualized rates from ProtocolPolicy
                    (deployed: rMin=0, rMax≈1.388 → ~200% APR target)

    Winners (side matches VS sign): earn at +r_lot APR (newly minted)
    Losers  (side opposes VS sign): lose at -r_lot APR (stake burned)

    If user_address is provided, the lot's actual positionWeight is
    used. Otherwise the function returns a side-level estimate
    (positionWeight=0.5, the sole-staker default).
    """
    R_MIN, R_MAX = _get_rate_bounds()

    try:
        support, challenge = get_stake_totals(post_id)
        total = support + challenge
        if total < 0.001:
            return 0.0

        vs = get_verity_score(post_id)
        if vs == 0:
            return 0.0  # no pressure at VS=0
        abs_vs = abs(vs)
        v = abs_vs / 100.0  # normalized truth pressure (0-1)

        # Get sMax from contract. If we cannot read it, return 0.0
        # rather than silently overstating — there is no honest
        # default for this value.
        try:
            se = _get_stake_engine()
            s_max_wei = se.functions.sMax().call()
            s_max = s_max_wei / 1e18
        except Exception as e:
            logger.warning("sMax read failed for APR estimate post %d: %s", post_id, e)
            return 0.0
        if s_max < 0.001:
            return 0.0

        participation = min(total / s_max, 1.0)
        r_base = R_MIN + (R_MAX - R_MIN) * v * participation

        # Per-lot positionWeight if a user is given, else 0.5 default.
        pos_weight = 0.5
        if user_address is not None:
            try:
                side_idx = 0 if side == "support" else 1
                lot = get_user_lot_info(user_address, post_id, side_idx)
                if lot is not None and lot.get("position_weight") is not None:
                    pos_weight = float(lot["position_weight"])
            except Exception:
                pass  # fall through to default

        r_lot = r_base * pos_weight

        support_wins = vs > 0
        is_winner = (side == "support" and support_wins) or (side == "challenge" and not support_wins)

        return r_lot * 100 if is_winner else -r_lot * 100

    except Exception as e:
        logger.warning("Failed to estimate APR for post %d: %s", post_id, e)
        return 0.0


def get_apr_breakdown(post_id, side="support"):
    """Return APR and its component factors for display.
    
    Returns dict with:
      apr: final APR percentage
      r_min, r_max: rate bounds
      vs: verity score
      abs_vs: absolute VS
      v: truth pressure (0-1)
      total_stake: post total stake
      s_max: system-wide max stake
      participation: post size factor (0-1)
      r_eff: effective rate before sign
      is_winner: whether this side is winning
    """
    R_MIN, R_MAX = _get_rate_bounds()
    
    result = {
        "apr": 0.0, "r_min": R_MIN * 100, "r_max": R_MAX * 100,
        "vs": 0.0, "abs_vs": 0.0, "v": 0.0,
        "total_stake": 0.0, "s_max": 0.0, "participation": 0.0,
        "r_eff": 0.0, "is_winner": False,
    }
    
    try:
        support, challenge = get_stake_totals(post_id)
        total = support + challenge
        result["total_stake"] = round(total, 4)
        
        if total < 0.001:
            return result
        
        vs = get_verity_score(post_id)
        abs_vs = abs(vs)
        v = abs_vs / 100.0
        result["vs"] = round(vs, 2)
        result["abs_vs"] = round(abs_vs, 2)
        result["v"] = round(v, 4)
        
        try:
            se = _get_stake_engine()
            s_max_wei = se.functions.sMax().call()
            s_max = s_max_wei / 1e18
        except Exception as e:
            logger.warning("sMax read failed for APR breakdown post %d: %s", post_id, e)
            return result  # leave participation/r_eff/apr at zero
        if s_max < 0.001:
            return result
        result["s_max"] = round(s_max, 4)

        participation = min(total / s_max, 1.0)
        result["participation"] = round(participation, 4)
        
        r_eff = R_MIN + (R_MAX - R_MIN) * v * participation
        result["r_eff"] = round(r_eff * 100, 2)
        
        support_wins = vs > 0
        is_winner = (side == "support" and support_wins) or (side == "challenge" and not support_wins)
        result["is_winner"] = is_winner
        
        if vs == 0:
            return result
        
        result["apr"] = round(r_eff * 100 if is_winner else -r_eff * 100, 1)
        return result
        
    except Exception as e:
        logger.warning("Failed to get APR breakdown for post %d: %s", post_id, e)
        return result


def get_user_lot_info(user_address, post_id, side):
    """Returns lot info: (amount, weightedPosition, entryEpoch, sideTotal, positionWeight).
    positionWeight is RAY-scaled (1e18 = 1.0 = best position)."""
    try:
        se = _get_stake_engine()
        addr = Web3.to_checksum_address(user_address)
        result = se.functions.getUserLotInfo(addr, post_id, side).call()
        return {
            "amount": result[0] / 1e18,
            "weighted_position": result[1] / 1e18,
            "entry_epoch": result[2],
            "side_total": result[3] / 1e18,
            "position_weight": result[4] / 1e18,  # 1.0 = best, 0.1 = worst
        }
    except Exception as e:
        logger.warning("Failed to get lot info for %s post %d side %d: %s",
                       user_address, post_id, side, e)
        return None


# ============================================================
# MM chain-state helpers (patch12a)
# ============================================================
#
# These functions derive vsp_circulating and usdc_reserves from
# chain state, replacing the tracked counters in mm_state. The
# tracked counters drift when VSP enters circulation through paths
# other than MM trades (yield mints, bounty mints) or when USDC
# enters reserves through donations.
#
# Caching: same 30s TTL as the rest of chain_reader.
# Failure mode: explicit exception. Callers must decide whether to
# fall back to a degraded mode or surface the error to the user.

# Standard ERC20 view ABI subset.
_ERC20_VIEW_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_vsp_token_contract = None
_usdc_token_contract = None


def _get_vsp_token():
    global _vsp_token_contract
    if _vsp_token_contract is None:
        from config import VSP_TOKEN_ADDRESS
        if not VSP_TOKEN_ADDRESS:
            raise RuntimeError("VSP_TOKEN_ADDRESS not configured")
        w3 = _get_w3()
        _vsp_token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
            abi=_ERC20_VIEW_ABI,
        )
    return _vsp_token_contract


def _get_usdc_token():
    global _usdc_token_contract
    if _usdc_token_contract is None:
        from config import USDC_ADDRESS
        if not USDC_ADDRESS:
            raise RuntimeError("USDC_ADDRESS not configured")
        w3 = _get_w3()
        _usdc_token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=_ERC20_VIEW_ABI,
        )
    return _usdc_token_contract


def read_vsp_circulating() -> float:
    """
    Returns the total VSP held outside the MM treasury, in VSP units
    (not wei). This is the size of the population that could
    potentially be sold back to the MM.

    vsp_circulating = vspToken.totalSupply() - vspToken.balanceOf(MM)

    Cached for _CACHE_TTL seconds. Raises on RPC failure — callers
    must handle. We do NOT silently return a stale value past TTL
    because mispriced sells could drain reserves.
    """
    def _read():
        from config import MM_ADDRESS
        if not MM_ADDRESS:
            raise RuntimeError("MM_ADDRESS not configured")
        vsp = _get_vsp_token()
        total_wei = vsp.functions.totalSupply().call()
        mm_balance_wei = vsp.functions.balanceOf(
            Web3.to_checksum_address(MM_ADDRESS)
        ).call()
        circulating_wei = max(0, total_wei - mm_balance_wei)
        # VSPToken uses 18 decimals (ERC20 default).
        return circulating_wei / 1e18
    return _cached("mm_vsp_circulating", _read)


def read_usdc_reserves() -> float:
    """
    Returns the virtual USDC reserves backing outstanding VSP, in
    USDC units (not micro-USDC). The virtual reserves are the sum
    of the hot MM wallet plus any configured cold-storage safes:

      usdc_reserves = balanceOf(MM) + sum(balanceOf(s) for s in COLD_SAFE_ADDRESSES)

    If no cold safes are configured (the default), this is just
    balanceOf(MM) — identical to pre-patch06 behaviour.

    Cached for _CACHE_TTL seconds. The cache is on the aggregate
    result, not the individual balanceOf calls; a single read
    triggers up to (1 + len(COLD_SAFE_ADDRESSES)) RPC calls. Raises
    on RPC failure (any single balanceOf failure raises the whole
    aggregate read — fail-fast, never partial sums).
    """
    def _read():
        from config import MM_ADDRESS, COLD_SAFE_ADDRESSES
        if not MM_ADDRESS:
            raise RuntimeError("MM_ADDRESS not configured")
        usdc = _get_usdc_token()
        total_micro = 0
        # Hot wallet
        total_micro += usdc.functions.balanceOf(
            Web3.to_checksum_address(MM_ADDRESS)
        ).call()
        # Cold safes (patch06; empty list when COLD_SAFE_ADDRESSES unset)
        for cold_addr in COLD_SAFE_ADDRESSES:
            total_micro += usdc.functions.balanceOf(
                Web3.to_checksum_address(cold_addr)
            ).call()
        # USDC uses 6 decimals.
        return total_micro / 1e6
    return _cached("mm_usdc_reserves", _read)


# patch_bundle04_6_balance_of_mm
def read_vsp_balance_of_mm() -> float:
    """
    Returns the live VSP balance of the MM hot wallet, in VSP units (not wei).
    UNCACHED — this is a guard read used immediately before signing a transfer,
    so staleness would defeat the guard. One RPC call per invocation.
    """
    from config import MM_ADDRESS
    if not MM_ADDRESS:
        raise RuntimeError("MM_ADDRESS not configured")
    vsp = _get_vsp_token()
    balance_wei = vsp.functions.balanceOf(
        Web3.to_checksum_address(MM_ADDRESS)
    ).call()
    return balance_wei / 1e18


def read_usdc_balance_of_mm() -> float:
    """
    Returns the live USDC balance of the MM hot wallet ONLY, in USDC units
    (not micro-USDC). Excludes cold-safe balances — those count toward virtual
    reserves for floor math, but the actual transfer() will draw from the hot
    wallet alone. UNCACHED for the same reason as read_vsp_balance_of_mm.
    """
    from config import MM_ADDRESS
    if not MM_ADDRESS:
        raise RuntimeError("MM_ADDRESS not configured")
    usdc = _get_usdc_token()
    balance_micro = usdc.functions.balanceOf(
        Web3.to_checksum_address(MM_ADDRESS)
    ).call()
    return balance_micro / 1e6


def invalidate_mm_chain_state_cache():
    """
    Force the next read to hit the chain. Call this after the relay
    submits an MM trade transaction, so subsequent quotes see the
    post-trade state immediately rather than waiting for the cache
    TTL to expire.
    """
    _cache.pop("mm_vsp_circulating", None)
    _cache.pop("mm_usdc_reserves", None)
