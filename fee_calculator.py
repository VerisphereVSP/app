"""
Dynamic fee calculator. Reads costs from DB, computes per-txn fees.

Fee = max(base_fee, value × pct / 10000) × (1 + margin / 10000)

ADDITIVE model:
  Buy:    user pays (qty × price) + fee_usdc,  receives qty VSP
  Sell:   user sends qty VSP,                   receives (qty × price) - fee_usdc
  Stake:  wallet debited qty + fee_vsp,         qty goes on-chain
  Create: wallet debited posting_fee + fee_vsp, posting_fee goes on-chain
"""
import logging
from time import time
from typing import Dict, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 300


def _load(db: Session) -> dict:
    now = time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    costs = {}
    for r in db.execute(sql_text("SELECT cost_key, monthly_usd FROM operating_costs")).fetchall():
        costs[r[0]] = float(r[1])

    params = {}
    for r in db.execute(sql_text("SELECT param_key, value FROM fee_params")).fetchall():
        params[r[0]] = r[1]

    total_monthly = sum(costs.values())
    expected_txns = max(int(params.get("expected_monthly_txns", "1000")), 1)
    vsp_price = max(float(params.get("vsp_price_usd", "1.30")), 0.01)
    pct_bps = int(params.get("pct_fee_bps", "100"))
    margin_bps = int(params.get("margin_bps", "3000"))
    enabled = params.get("fee_enabled", "true").lower() == "true"

    base_fee_vsp = (total_monthly / expected_txns) / vsp_price

    data = {
        "costs": costs,
        "total_monthly_usd": round(total_monthly, 2),
        "expected_monthly_txns": expected_txns,
        "vsp_price_usd": vsp_price,
        "per_txn_cost_usd": round(total_monthly / expected_txns, 4),
        "base_fee_vsp": round(base_fee_vsp, 4),
        "pct_fee_bps": pct_bps,
        "margin_bps": margin_bps,
        "enabled": enabled,
    }
    _cache["data"] = data
    _cache["ts"] = now
    return data


def invalidate_cache():
    _cache["data"] = None
    _cache["ts"] = 0


def compute_fee(db: Session, tx_type: str, value_vsp: float) -> dict:
    """Compute fee for a transaction. Returns fee breakdown."""
    fp = _load(db)
    if not fp["enabled"]:
        return {"fee_vsp": 0, "fee_usd": 0, "base_fee": 0, "pct_fee": 0,
                "margin": 0, "breakdown": "Fees disabled"}

    base = fp["base_fee_vsp"]
    pct = value_vsp * fp["pct_fee_bps"] / 10_000
    cost_fee = max(base, pct)
    margin = cost_fee * fp["margin_bps"] / 10_000
    total = cost_fee + margin
    total_usd = total * fp["vsp_price_usd"]

    return {
        "fee_vsp": round(total, 6),
        "fee_usd": round(total_usd, 4),
        "base_fee": round(base, 6),
        "pct_fee": round(pct, 6),
        "margin": round(margin, 6),
        "breakdown": f"{'base' if base >= pct else 'pct'}: {cost_fee:.4f} + {fp['margin_bps']/100:.0f}% margin: {margin:.4f} = {total:.4f} VSP (${total_usd:.2f})",
    }


def get_fee_schedule(db: Session) -> dict:
    """Full fee schedule for API display."""
    fp = _load(db)
    fp["examples"] = {
        "buy_1_vsp": compute_fee(db, "buy", 1),
        "buy_10_vsp": compute_fee(db, "buy", 10),
        "buy_100_vsp": compute_fee(db, "buy", 100),
        "stake_1_vsp": compute_fee(db, "stake", 1),
        "create_claim": compute_fee(db, "create", 1),
    }
    return fp


# ═══════════════════════════════════════════════════════════════
# Relay fee: strictly gas × one markup knob — patch_relay_gas_fee
# ═══════════════════════════════════════════════════════════════
#
#   fee_VSP = gas_units × gas_price × AVAX_USD ÷ VSP_USD × (1 + RELAY_FEE_MARKUP)
#
# gas_units already includes the collection transfer (RELAY_FEE_COLLECT_GAS), so
# collecting the fee pays for itself. No floor, no percentage of the action's
# value: a 1 VSP stake and a 1,000 VSP stake pay the same few tenths of a VSP.
# The two prices are measurements with a last-good memory of
# RELAY_FEE_PRICE_MAX_AGE_SEC; with no usable price the quote REFUSES
# (FeeOracleUnavailable) instead of guessing. RELAY_FEE_CAP_VSP is a safety
# ceiling against a broken price read, not a pricing term.

import time as _time


class FeeOracleUnavailable(RuntimeError):
    """No price younger than RELAY_FEE_PRICE_MAX_AGE_SEC for AVAX or VSP."""


_last_good = {"avax": (None, 0.0), "vsp": (None, 0.0)}  # name -> (price, ts)


def _remember(name: str, price):
    if price and price > 0:
        _last_good[name] = (float(price), _time.time())
        return float(price)
    return None


def _recall(name: str):
    from config import RELAY_FEE_PRICE_MAX_AGE_SEC
    price, ts = _last_good[name]
    if price and _time.time() - ts <= RELAY_FEE_PRICE_MAX_AGE_SEC:
        return price
    return None


_avax_cache = {"price": None, "ts": 0}


def _get_avax_price_usd() -> float:
    """AVAX/USD from the unified oracle (Chainlink AVAX/USD → CoinGecko → config
    fallback, patch_bundle08_oracle_avax_unify), cached 60 s. Remembers the last
    good value for the quote's last-good window."""
    now = _time.time()
    if _avax_cache["price"] and now - _avax_cache["ts"] < 60:
        return _avax_cache["price"]
    try:
        from mm.oracle import get_avax_price_usd as _oracle_avax
        price = _oracle_avax()
        if price and price > 0:
            _avax_cache["price"] = price
            _avax_cache["ts"] = now
            _remember("avax", price)
            return price
    except Exception:
        pass
    from config import AVAX_PRICE_USD
    return AVAX_PRICE_USD


def _get_vsp_price_usd(db: Session = None) -> float:
    """VSP price in USDC from the pool reserves (chain/pool_price, 15 s cache).
    patch_relay_gas_fee: the previous reader used the market-maker's state
    (usdc_reserves / vsp_circulating), retired with the MM, and silently fell
    back to a hard-coded 1.30. Now the pool, or the last good pool read within
    the quote window; raises FeeOracleUnavailable past that. `db` is kept for
    caller compatibility and unused."""
    try:
        from chain.pool_price import read_pool_state
        st = read_pool_state()
        price = float(st.get("price_usdc_per_vsp") or 0)
        if price > 0:
            return _remember("vsp", price)
    except Exception as e:
        logging.getLogger(__name__).warning("VSP pool price read failed: %s", e)
    recalled = _recall("vsp")
    if recalled is None:
        raise FeeOracleUnavailable("VSP price: no pool read within the last-good window")
    return recalled


def billed_gas(limit: int, used_estimate: int) -> int:
    """patch_relay_gas_fee_4: what Avalanche bills for a transaction — max(gas used, gas limit / 2).
    Proven 2026-10-11: a 21,000-gas self-transfer with a 1,000,000 limit was billed 500,000
    (Fuji 0x93201ba4…); the relay's stakes with a 1,600,000 limit were billed exactly 800,000."""
    return max(int(used_estimate), int(limit) // 2)


def quote_relay_fee(gas_units: int, gas_price_wei: int, onchain_fee_vsp: float = 0.0) -> dict:
    """The one formula. `gas_units` is the full relay cost in gas (execute +
    collection). `onchain_fee_vsp` is whatever the Forwarder still charges by
    itself (transition only; 0 once governance has switched it off) — it is
    subtracted so the user pays exactly the quote either way.

    Returns fee_vsp (what the relay collects), fee_total_vsp (what the user
    pays in total, = quote), and every component for the log and the UI.
    Raises FeeOracleUnavailable when a price is missing."""
    from config import (RELAY_FEE_ENABLED, RELAY_FEE_MARKUP, RELAY_FEE_CAP_VSP)
    avax_usd = _get_avax_price_usd()
    if not avax_usd or avax_usd <= 0:
        avax_usd = _recall("avax")
        if avax_usd is None:
            raise FeeOracleUnavailable("AVAX price unavailable")
    vsp_usd = _get_vsp_price_usd()

    gas_cost_avax = (int(gas_units) * int(gas_price_wei)) / 1e18
    gas_cost_usd = gas_cost_avax * avax_usd
    gas_cost_vsp = gas_cost_usd / vsp_usd
    total_vsp = gas_cost_vsp * (1.0 + RELAY_FEE_MARKUP)
    capped = False
    if total_vsp > RELAY_FEE_CAP_VSP:
        total_vsp, capped = RELAY_FEE_CAP_VSP, True
    if not RELAY_FEE_ENABLED:
        total_vsp = 0.0
    relay_part = max(0.0, total_vsp - float(onchain_fee_vsp or 0.0))
    return {
        "fee_vsp": round(relay_part, 6),            # collected by the relay (transferFrom)
        "fee_total_vsp": round(total_vsp, 6),       # what the user pays in total = the displayed quote
        "fee_usd": round(total_vsp * vsp_usd, 4),
        "onchain_fee_vsp": round(float(onchain_fee_vsp or 0.0), 6),
        "gas_units": int(gas_units),
        "gas_price_gwei": round(int(gas_price_wei) / 1e9, 3),
        "gas_cost_avax": round(gas_cost_avax, 8),
        "gas_cost_usd": round(gas_cost_usd, 4),
        "gas_cost_vsp": round(gas_cost_vsp, 6),
        "avax_price_usd": round(avax_usd, 4),
        "vsp_price_usd": round(vsp_usd, 6),
        "markup": RELAY_FEE_MARKUP,
        "capped": capped,
        "enabled": RELAY_FEE_ENABLED,
    }


def is_fee_exempt(db: Session, address: str) -> bool:
    """Check if address is on the fee-exempt allow-list."""
    try:
        row = db.execute(sql_text(
            "SELECT 1 FROM fee_exempt_addresses WHERE address = :addr"
        ), {"addr": address.lower()}).fetchone()
        return row is not None
    except Exception:
        return False
