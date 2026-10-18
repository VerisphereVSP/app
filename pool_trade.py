"""app/pool_trade.py — patch_pool_trade

Buy and sell VSP against the public UniswapV2-interface pool, paying with AVAX or USDC, in ONE transaction.

    buy  with AVAX : router.swapExact<Native>ForTokens(minOut, [WAVAX, USDC, VSP], to, deadline)  value = AVAX in — no approval
    buy  with USDC : router.swapExactTokensForTokens(amountIn, minOut, [USDC, VSP], to, deadline)   approve USDC → router
    sell for  AVAX : router.swapExactTokensFor<Native>(amountIn, minOut, [VSP, USDC, WAVAX], to, deadline)  approve VSP → router
    sell for  USDC : router.swapExactTokensForTokens(amountIn, minOut, [VSP, USDC], to, deadline)   approve VSP → router

The AVAX routes hop through the venue's WAVAX/USDC pair (deep on both Uniswap v2 mainnet and Joe V1 Fuji); there is no second
VSP pool, so one pool keeps all the depth and VSP has one price. The venue's native-coin function names differ — Uniswap v2
Router02 keeps `swapExactETHForTokens`/`WETH()` on Avalanche, JoeRouter02 says `swapExactAVAXForTokens`/`WAVAX()` — so the
flavour is detected once from the router (`WETH()` answers or `WAVAX()` does) and served to clients with the selectors.

Three endpoints (all read-only on chain; the USER signs and pays gas — the relay is not involved, a DEX buyer has AVAX):
    GET  /api/pool/routes                     addresses, native flavour + selectors, which routes are live, slippage bounds
    GET  /api/pool/quote?side=&pay_with=&amount=   expected output, minimum at the given slippage, price impact, path
    POST /api/pool/build  {side, pay_with, amount, recipient, slippage_bps?, deadline_s?}
                                              {to, data, value, path, amountIn, minOut, deadline, approve?: {token, spender, amount}}
The client decodes and checks the calldata before signing (the extension's verifyBuild does; the web app's hook does).

Pure planning (paths, minimum-out, price impact, calldata encoding against a fixed ABI) is separated from chain reads so it is
unit-tested without an RPC: `plan()` and `encode()` take reserves and addresses as arguments.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from web3 import Web3

from config import CHAIN_ID, POOL_PAIR_ADDRESS, POOL_VENUE, RPC_URL_READ, USDC_ADDRESS, VENUE_ROUTER, VSP_ADDRESS
from rate_limit import public_endpoint

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pool", tags=["pool"])

VSP_DECIMALS = 18
USDC_DECIMALS = 6
AVAX_DECIMALS = 18
FEE_BPS_PER_HOP = 30                  # UniswapV2 / Joe V1: 0.3 % per pool
DEFAULT_SLIPPAGE_BPS = 50             # 0.5 %
MAX_SLIPPAGE_BPS = 500                # 5 % — above this the client is asking to be sandwiched
DEFAULT_DEADLINE_S = 20 * 60
MAX_DEADLINE_S = 60 * 60
MAX_AMOUNT_IN = {"AVAX": 10_000.0, "USDC": 1_000_000.0, "VSP": 10_000_000.0}   # sanity caps, not policy
# Canonical wrapped-native per chain; env WAVAX_ADDRESS overrides. Validated against the router's WETH()/WAVAX() on first use.
_WAVAX_DEFAULT = {43114: "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", 43113: "0xd00ae08403B9bbb9124bB305C09058E32C39A48c"}
WAVAX_ADDRESS = (os.getenv("WAVAX_ADDRESS", "") or _WAVAX_DEFAULT.get(CHAIN_ID, "")).strip()

# ── ABI: one router ABI per native flavour (ETH names = Uniswap v2 Router02, AVAX names = JoeRouter02) ─────────────────
def _router_abi(native: str) -> list:
    n = native  # "ETH" | "AVAX"
    path = {"name": "path", "type": "address[]"}
    return [
        {"type": "function", "name": f"W{n}", "inputs": [], "outputs": [{"name": "", "type": "address"}], "stateMutability": "pure"},
        {"type": "function", "name": "factory", "inputs": [], "outputs": [{"name": "", "type": "address"}], "stateMutability": "pure"},
        {"type": "function", "name": "getAmountsOut", "stateMutability": "view",
         "inputs": [{"name": "amountIn", "type": "uint256"}, path], "outputs": [{"name": "amounts", "type": "uint256[]"}]},
        {"type": "function", "name": f"swapExact{n}ForTokens", "stateMutability": "payable",
         "inputs": [{"name": "amountOutMin", "type": "uint256"}, path, {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
         "outputs": [{"name": "amounts", "type": "uint256[]"}]},
        {"type": "function", "name": f"swapExactTokensFor{n}", "stateMutability": "nonpayable",
         "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"}, path, {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
         "outputs": [{"name": "amounts", "type": "uint256[]"}]},
        {"type": "function", "name": "swapExactTokensForTokens", "stateMutability": "nonpayable",
         "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"}, path, {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
         "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    ]


_FACTORY_ABI = [{"type": "function", "name": "getPair", "stateMutability": "view",
                 "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}], "outputs": [{"name": "", "type": "address"}]}]
_PAIR_ABI = [
    {"type": "function", "name": "getReserves", "inputs": [], "stateMutability": "view",
     "outputs": [{"name": "r0", "type": "uint112"}, {"name": "r1", "type": "uint112"}, {"name": "ts", "type": "uint32"}]},
    {"type": "function", "name": "token0", "inputs": [], "stateMutability": "view", "outputs": [{"name": "", "type": "address"}]},
]


def selectors(native: str) -> Dict[str, str]:
    """4-byte selectors the clients verify against (hex, no 0x)."""
    def sel(sig: str) -> str:
        return Web3.keccak(text=sig)[:4].hex()
    n = native
    return {
        "swapExactNativeForTokens": sel(f"swapExact{n}ForTokens(uint256,address[],address,uint256)"),
        "swapExactTokensForNative": sel(f"swapExactTokensFor{n}(uint256,uint256,address[],address,uint256)"),
        "swapExactTokensForTokens": sel("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"),
    }


# ── pure planning ────────────────────────────────────────────────────────────────────────────────────────────────────
def amount_out_v2(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    """UniswapV2Library.getAmountOut (0.3 % fee)."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    with_fee = amount_in * 997
    return (with_fee * reserve_out) // (reserve_in * 1000 + with_fee)


def path_for(side: str, pay_with: str, wavax: str, usdc: str, vsp: str) -> List[str]:
    """Token path for the trade. `pay_with` is the user's other asset: what they pay (buy) or receive (sell)."""
    if side == "buy":
        return [wavax, usdc, vsp] if pay_with == "AVAX" else [usdc, vsp]
    return [vsp, usdc, wavax] if pay_with == "AVAX" else [vsp, usdc]


def plan(side: str, pay_with: str, amount_in: int, reserves: List[tuple], slippage_bps: int) -> Dict[str, Any]:
    """amount_in in the input token's smallest unit; reserves = [(reserve_in, reserve_out)] per hop in path order.
    Returns amounts per hop, minOut at slippage, spot output (no impact) and price impact in bps (fee excluded)."""
    if side not in ("buy", "sell") or pay_with not in ("AVAX", "USDC"):
        raise ValueError("side must be buy|sell and pay_with AVAX|USDC")
    if not (0 <= slippage_bps <= MAX_SLIPPAGE_BPS):
        raise ValueError(f"slippage_bps must be within 0..{MAX_SLIPPAGE_BPS}")
    if amount_in <= 0:
        raise ValueError("amount must be positive")
    amounts = [amount_in]
    spot = amount_in
    for r_in, r_out in reserves:
        amounts.append(amount_out_v2(amounts[-1], r_in, r_out))
        spot = spot * r_out * 997 // (r_in * 1000)        # same fee, no slippage: the marginal price
    out = amounts[-1]
    if out <= 0:
        raise ValueError("no output for this amount (empty pool?)")
    impact_bps = max(0, (spot - out) * 10_000 // spot) if spot > 0 else 0
    min_out = out * (10_000 - slippage_bps) // 10_000
    return {"amounts": amounts, "amountOut": out, "minOut": min_out, "spotOut": spot, "priceImpactBps": impact_bps,
            "feeBps": FEE_BPS_PER_HOP * len(reserves), "hops": len(reserves)}


def encode(native: str, router_addr: str, side: str, pay_with: str, path: List[str], amount_in: int, min_out: int,
           recipient: str, deadline: int) -> Dict[str, Any]:
    """Calldata for the swap (offline; no provider needed). Returns {to, data, value, fn}."""
    c = Web3().eth.contract(address=Web3.to_checksum_address(router_addr), abi=_router_abi(native))
    cpath = [Web3.to_checksum_address(a) for a in path]
    to = Web3.to_checksum_address(recipient)
    if side == "buy" and pay_with == "AVAX":
        fn, args, value = f"swapExact{native}ForTokens", [min_out, cpath, to, deadline], amount_in
    elif side == "sell" and pay_with == "AVAX":
        fn, args, value = f"swapExactTokensFor{native}", [amount_in, min_out, cpath, to, deadline], 0
    else:
        fn, args, value = "swapExactTokensForTokens", [amount_in, min_out, cpath, to, deadline], 0
    enc = getattr(c, "encode_abi", None)
    try:
        data = enc(fn, args=args) if enc else c.encodeABI(fn_name=fn, args=args)
    except TypeError:
        data = enc(abi_element_identifier=fn, args=args)
    return {"to": router_addr.lower(), "data": data, "value": str(value), "fn": fn}


def to_units(amount: float, symbol: str) -> int:
    dec = {"AVAX": AVAX_DECIMALS, "USDC": USDC_DECIMALS, "VSP": VSP_DECIMALS}[symbol]
    return int(round(float(amount) * 10 ** dec))


def from_units(wei: int, symbol: str) -> float:
    dec = {"AVAX": AVAX_DECIMALS, "USDC": USDC_DECIMALS, "VSP": VSP_DECIMALS}[symbol]
    return wei / 10 ** dec


# ── chain reads (cached briefly) ──────────────────────────────────────────────────────────────────────────────────────
_state: Dict[str, Any] = {"w3": None, "native": None, "router": None, "wavax_pair": None, "checked": 0.0}
_CACHE_TTL = 10.0


def _w3():
    if _state["w3"] is None:
        from chain.provider import w3 as shared
        _state["w3"] = shared
    return _state["w3"]


def configured() -> bool:
    return bool(POOL_PAIR_ADDRESS and VENUE_ROUTER and POOL_VENUE == "univ2" and WAVAX_ADDRESS)


def _detect_native() -> str:
    """Ask the router which flavour it speaks; validate WAVAX_ADDRESS against it. Cached for the process."""
    if _state["native"]:
        return _state["native"]
    w3 = _w3()
    for native in ("ETH", "AVAX"):
        try:
            c = w3.eth.contract(address=Web3.to_checksum_address(VENUE_ROUTER), abi=_router_abi(native))
            w = getattr(c.functions, f"W{native}")().call()
        except Exception:
            continue
        if w.lower() != WAVAX_ADDRESS.lower():
            raise RuntimeError(f"pool_trade: router W{native}() = {w} but WAVAX_ADDRESS = {WAVAX_ADDRESS}")
        _state["native"], _state["router"] = native, c
        logger.info("pool_trade: router %s speaks %s names; wrapped native %s", VENUE_ROUTER, native, w)
        return native
    raise RuntimeError("pool_trade: router answers neither WETH() nor WAVAX()")


def _reserves(pair_addr: str, token_in: str, token_out: str) -> tuple:
    """(reserve_in, reserve_out) of a pair oriented for token_in → token_out."""
    w3 = _w3()
    p = w3.eth.contract(address=Web3.to_checksum_address(pair_addr), abi=_PAIR_ABI)
    r0, r1, _ = p.functions.getReserves().call()
    t0 = p.functions.token0().call()
    return (r0, r1) if t0.lower() == token_in.lower() else (r1, r0)


def _wavax_pair() -> Optional[str]:
    if _state["wavax_pair"] is None:
        _detect_native()
        f = _w3().eth.contract(address=Web3.to_checksum_address(_state["router"].functions.factory().call()), abi=_FACTORY_ABI)
        pair = f.functions.getPair(Web3.to_checksum_address(WAVAX_ADDRESS), Web3.to_checksum_address(USDC_ADDRESS)).call()
        _state["wavax_pair"] = pair if int(pair, 16) != 0 else ""
    return _state["wavax_pair"] or None


def _hop_reserves(path: List[str]) -> List[tuple]:
    """Reserves per hop, oriented. Hops touching WAVAX use the venue's WAVAX/USDC pair; the VSP hop uses our pair."""
    out = []
    for a, b in zip(path, path[1:]):
        pair = POOL_PAIR_ADDRESS if {a.lower(), b.lower()} == {VSP_ADDRESS.lower(), USDC_ADDRESS.lower()} else _wavax_pair()
        if not pair:
            raise HTTPException(503, "AVAX route unavailable: the venue has no WAVAX/USDC pair")
        out.append(_reserves(pair, a, b))
    return out


def _routes() -> Dict[str, Any]:
    """Live route table for clients; cached 10 s."""
    now = time.time()
    if _state.get("routes") and now - _state["checked"] < _CACHE_TTL:
        return _state["routes"]
    native = _detect_native()
    avax_ok = False
    try:
        pair = _wavax_pair()
        if pair:
            r_in, r_out = _reserves(pair, WAVAX_ADDRESS, USDC_ADDRESS)
            avax_ok = r_in > 0 and r_out > 0
    except Exception as e:  # the USDC route still works
        logger.warning("pool_trade: WAVAX/USDC pair check failed: %s", e)
    _state["routes"] = {
        "configured": True, "chainId": CHAIN_ID, "router": VENUE_ROUTER.lower(), "native": native,
        "selectors": selectors(native),
        "tokens": {"wavax": WAVAX_ADDRESS.lower(), "usdc": USDC_ADDRESS.lower(), "vsp": VSP_ADDRESS.lower()},
        "pairs": {"vsp_usdc": POOL_PAIR_ADDRESS.lower(), "wavax_usdc": (_state["wavax_pair"] or "").lower() or None},
        "routes": {"AVAX": avax_ok, "USDC": True},
        "feeBpsPerHop": FEE_BPS_PER_HOP, "defaultSlippageBps": DEFAULT_SLIPPAGE_BPS, "maxSlippageBps": MAX_SLIPPAGE_BPS,
        "defaultDeadlineS": DEFAULT_DEADLINE_S,
    }
    _state["checked"] = now
    return _state["routes"]


def _parse(side: Any, pay_with: Any, amount: Any) -> tuple:
    side = str(side or "").lower(); pay_with = str(pay_with or "AVAX").upper()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be 'buy' or 'sell'")
    if pay_with not in ("AVAX", "USDC"):
        raise HTTPException(400, "pay_with must be 'AVAX' or 'USDC'")
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be a number")
    sym_in = pay_with if side == "buy" else "VSP"
    if not (0 < amt <= MAX_AMOUNT_IN[sym_in]):
        raise HTTPException(400, f"amount must be between 0 and {MAX_AMOUNT_IN[sym_in]} {sym_in}")
    return side, pay_with, amt, sym_in, ("VSP" if side == "buy" else pay_with)


def _quote(side: str, pay_with: str, amt: float, sym_in: str, sym_out: str, slippage_bps: int) -> Dict[str, Any]:
    routes = _routes()
    if pay_with == "AVAX" and not routes["routes"]["AVAX"]:
        raise HTTPException(503, "AVAX route unavailable on this venue; pay with USDC")
    path = path_for(side, pay_with, WAVAX_ADDRESS, USDC_ADDRESS, VSP_ADDRESS)
    amount_in = to_units(amt, sym_in)
    try:
        p = plan(side, pay_with, amount_in, _hop_reserves(path), slippage_bps)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "side": side, "payWith": pay_with, "path": [a.lower() for a in path],
        "amountIn": str(amount_in), "amountInHuman": amt, "symbolIn": sym_in, "symbolOut": sym_out,
        "amountOut": str(p["amountOut"]), "amountOutHuman": from_units(p["amountOut"], sym_out),
        "minOut": str(p["minOut"]), "minOutHuman": from_units(p["minOut"], sym_out),
        "priceImpactBps": p["priceImpactBps"], "feeBps": p["feeBps"], "hops": p["hops"], "slippageBps": slippage_bps,
        "native": routes["native"], "router": routes["router"],
    }


# ── endpoints ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/routes")
@public_endpoint("/api/pool/routes", cost_tier="cheap")
def pool_routes(request: Request) -> Dict[str, Any]:
    if not configured():
        return {"configured": False}
    try:
        return _routes()
    except Exception as e:
        logger.warning("pool/routes failed: %s", e)
        raise HTTPException(503, "pool routes unavailable")


@router.get("/quote")
@public_endpoint("/api/pool/quote", cost_tier="cheap")
def pool_quote(request: Request, side: str, amount: float, pay_with: str = "AVAX", slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> Dict[str, Any]:
    if not configured():
        raise HTTPException(503, "pool trading not configured")
    s, pw, amt, sym_in, sym_out = _parse(side, pay_with, amount)
    if not (0 <= int(slippage_bps) <= MAX_SLIPPAGE_BPS):
        raise HTTPException(400, f"slippage_bps must be within 0..{MAX_SLIPPAGE_BPS}")
    return _quote(s, pw, amt, sym_in, sym_out, int(slippage_bps))


@router.post("/build")
@public_endpoint("/api/pool/build", cost_tier="cheap")
def pool_build(body: dict, request: Request) -> Dict[str, Any]:
    """{side, pay_with, amount, recipient, slippage_bps?, deadline_s?} → the transaction the user signs (+ approval if any)."""
    if not configured():
        raise HTTPException(503, "pool trading not configured")
    s, pw, amt, sym_in, sym_out = _parse(body.get("side"), body.get("pay_with"), body.get("amount"))
    recipient = body.get("recipient")
    if not isinstance(recipient, str) or not Web3.is_address(recipient):
        raise HTTPException(400, "recipient must be a valid address (the connected wallet)")
    try:
        slippage_bps = int(body.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)); deadline_s = int(body.get("deadline_s", DEFAULT_DEADLINE_S))
    except (TypeError, ValueError):
        raise HTTPException(400, "slippage_bps and deadline_s must be integers")
    if not (0 <= slippage_bps <= MAX_SLIPPAGE_BPS):
        raise HTTPException(400, f"slippage_bps must be within 0..{MAX_SLIPPAGE_BPS}")
    if not (60 <= deadline_s <= MAX_DEADLINE_S):
        raise HTTPException(400, f"deadline_s must be within 60..{MAX_DEADLINE_S}")
    q = _quote(s, pw, amt, sym_in, sym_out, slippage_bps)
    deadline = int(time.time()) + deadline_s
    tx = encode(q["native"], VENUE_ROUTER, s, pw, q["path"], int(q["amountIn"]), int(q["minOut"]), recipient, deadline)
    out = {**tx, "path": q["path"], "amountIn": q["amountIn"], "minOut": q["minOut"], "deadline": deadline, "quote": q}
    # what the user must approve first (the client checks the live allowance and skips when covered)
    if not (s == "buy" and pw == "AVAX"):
        token_in = USDC_ADDRESS if (s == "buy") else VSP_ADDRESS
        out["approve"] = {"token": token_in.lower(), "spender": VENUE_ROUTER.lower(), "amount": q["amountIn"]}
    return out
