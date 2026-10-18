# tests/test_pool_trade.py — patch_pool_trade
"""Buy/sell VSP with AVAX or USDC in one router transaction. The planning (paths, UniswapV2 amount-out, minimum at
slippage, price impact) and the calldata encoding are pure and tested here against the formula and the selectors; the
endpoints are exercised with the chain reads faked (no RPC)."""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pool_trade as pt  # noqa: E402

WAVAX = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"
USDC = "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E"
VSP = "0x0273B4D58E8991f71fC28d0377424d03d7c3b173"
ROUTER = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
USER = "0xf7a344aA311Cbb58BDA4C7953618675BCBa284a6"
# mainnet pool after the 2026-10-18 deepen, and a plausible WAVAX/USDC pool (~$25 AVAX)
R_VSP, R_USDC = 83_157_738_673_947_739_798_496, 16_843_273_073
R_WAVAX, R_USDC2 = 400_000 * 10**18, 10_000_000 * 10**6


def test_amount_out_matches_uniswap_v2_formula():
    a = 1000 * 10**6
    expect = (a * 997 * R_VSP) // (R_USDC * 1000 + a * 997)
    assert pt.amount_out_v2(a, R_USDC, R_VSP) == expect
    assert pt.amount_out_v2(0, R_USDC, R_VSP) == 0 and pt.amount_out_v2(a, 0, R_VSP) == 0


def test_paths():
    assert pt.path_for("buy", "AVAX", WAVAX, USDC, VSP) == [WAVAX, USDC, VSP]
    assert pt.path_for("buy", "USDC", WAVAX, USDC, VSP) == [USDC, VSP]
    assert pt.path_for("sell", "AVAX", WAVAX, USDC, VSP) == [VSP, USDC, WAVAX]
    assert pt.path_for("sell", "USDC", WAVAX, USDC, VSP) == [VSP, USDC]


def test_plan_two_hops_fee_impact_and_min_out():
    one_avax = 10**18
    p = pt.plan("buy", "AVAX", one_avax, [(R_WAVAX, R_USDC2), (R_USDC, R_VSP)], slippage_bps=50)
    assert p["hops"] == 2 and p["feeBps"] == 60
    # 1 AVAX ≈ 25 USDC ≈ 123 VSP at 0.2025; the pool is thin, so a little impact, well under 1 %
    assert 110 * 10**18 < p["amountOut"] < 130 * 10**18
    assert 0 <= p["priceImpactBps"] < 100
    assert p["minOut"] == p["amountOut"] * 9950 // 10_000
    # a large buy has a much larger impact than a small one
    big = pt.plan("buy", "AVAX", 1000 * one_avax, [(R_WAVAX, R_USDC2), (R_USDC, R_VSP)], 50)
    assert big["priceImpactBps"] > 5000


def test_plan_rejects_bad_inputs():
    with pytest.raises(ValueError):
        pt.plan("buy", "AVAX", 0, [(1, 1)], 50)
    with pytest.raises(ValueError):
        pt.plan("buy", "AVAX", 10**18, [(R_WAVAX, R_USDC2)], pt.MAX_SLIPPAGE_BPS + 1)
    with pytest.raises(ValueError):
        pt.plan("hold", "AVAX", 10**18, [(1, 1)], 50)


@pytest.mark.parametrize("native", ["ETH", "AVAX"])
def test_encode_selectors_value_and_args(native):
    sels = pt.selectors(native)
    dl = 1_800_000_000
    # buy with native: payable, value = amountIn, selector swapExact<N>ForTokens
    tx = pt.encode(native, ROUTER, "buy", "AVAX", [WAVAX, USDC, VSP], 10**18, 123 * 10**18, USER, dl)
    assert tx["to"] == ROUTER.lower() and tx["value"] == str(10**18) and tx["data"][2:10] == sels["swapExactNativeForTokens"]
    assert tx["fn"] == f"swapExact{native}ForTokens"
    # decode back: minOut, path, to, deadline
    c = Web3().eth.contract(address=Web3.to_checksum_address(ROUTER), abi=pt._router_abi(native))
    fn, args = c.decode_function_input(tx["data"])
    assert args["amountOutMin"] == 123 * 10**18 and [a.lower() for a in args["path"]] == [WAVAX.lower(), USDC.lower(), VSP.lower()]
    assert args["to"].lower() == USER.lower() and args["deadline"] == dl
    # sell for native: value 0, selector swapExactTokensFor<N>
    tx2 = pt.encode(native, ROUTER, "sell", "AVAX", [VSP, USDC, WAVAX], 100 * 10**18, 7 * 10**17, USER, dl)
    assert tx2["value"] == "0" and tx2["data"][2:10] == sels["swapExactTokensForNative"]
    # token-token: same selector on both venues
    tx3 = pt.encode(native, ROUTER, "buy", "USDC", [USDC, VSP], 100 * 10**6, 400 * 10**18, USER, dl)
    assert tx3["value"] == "0" and tx3["data"][2:10] == sels["swapExactTokensForTokens"]


def test_selectors_are_the_known_uniswap_and_joe_ones():
    assert pt.selectors("ETH")["swapExactNativeForTokens"] == "7ff36ab5"   # swapExactETHForTokens
    assert pt.selectors("ETH")["swapExactTokensForNative"] == "18cbafe5"   # swapExactTokensForETH
    assert pt.selectors("ETH")["swapExactTokensForTokens"] == "38ed1739"
    assert pt.selectors("AVAX")["swapExactNativeForTokens"] == "a2a1623d"  # swapExactAVAXForTokens (Joe)
    assert pt.selectors("AVAX")["swapExactTokensForNative"] == "676528d1"  # swapExactTokensForAVAX (Joe)


@pytest.fixture
def faked(monkeypatch):
    """Endpoints with the chain faked: mainnet-shaped config, ETH-flavoured router, both pairs funded."""
    monkeypatch.setattr(pt, "POOL_PAIR_ADDRESS", "0x853a92183c33486c77374aD748db4812742b092D")
    monkeypatch.setattr(pt, "VENUE_ROUTER", ROUTER)
    monkeypatch.setattr(pt, "POOL_VENUE", "univ2")
    monkeypatch.setattr(pt, "WAVAX_ADDRESS", WAVAX)
    monkeypatch.setattr(pt, "USDC_ADDRESS", USDC)
    monkeypatch.setattr(pt, "VSP_ADDRESS", VSP)
    monkeypatch.setattr(pt, "_detect_native", lambda: "ETH")
    monkeypatch.setattr(pt, "_wavax_pair", lambda: "0x" + "a" * 40)

    def hop_reserves(path):
        out = []
        for a, b in zip(path, path[1:]):
            pair = {frozenset({WAVAX.lower(), USDC.lower()}): (WAVAX, R_WAVAX, USDC, R_USDC2),
                    frozenset({USDC.lower(), VSP.lower()}): (USDC, R_USDC, VSP, R_VSP)}[frozenset({a.lower(), b.lower()})]
            t_in, r_in, t_out, r_out = pair
            out.append((r_in, r_out) if a.lower() == t_in.lower() else (r_out, r_in))
        return out
    monkeypatch.setattr(pt, "_hop_reserves", hop_reserves)
    monkeypatch.setattr(pt, "_reserves", lambda pair, a, b: (R_WAVAX, R_USDC2))
    pt._state["routes"] = None; pt._state["checked"] = 0.0
    return pt


def test_routes_endpoint(faked):
    r = faked.pool_routes(request=None)
    assert r["configured"] and r["native"] == "ETH" and r["routes"] == {"AVAX": True, "USDC": True}
    assert r["tokens"]["wavax"] == WAVAX.lower() and r["selectors"]["swapExactNativeForTokens"] == "7ff36ab5"


def test_quote_and_build_buy_with_avax(faked):
    q = faked.pool_quote(request=None, side="buy", amount=1.0, pay_with="AVAX")
    assert q["symbolIn"] == "AVAX" and q["symbolOut"] == "VSP" and q["hops"] == 2 and 110 < q["amountOutHuman"] < 130
    b = faked.pool_build({"side": "buy", "pay_with": "AVAX", "amount": 1.0, "recipient": USER}, request=None)
    assert b["to"] == ROUTER.lower() and b["value"] == str(10**18) and b["data"][2:10] == "7ff36ab5"
    assert "approve" not in b and b["minOut"] == q["minOut"]
    assert b["deadline"] > 0


def test_build_buy_with_usdc_and_sell_need_approval(faked):
    b = faked.pool_build({"side": "buy", "pay_with": "USDC", "amount": 100, "recipient": USER}, request=None)
    assert b["approve"] == {"token": USDC.lower(), "spender": ROUTER.lower(), "amount": str(100 * 10**6)} and b["value"] == "0"
    s = faked.pool_build({"side": "sell", "pay_with": "AVAX", "amount": 50, "recipient": USER, "slippage_bps": 100}, request=None)
    assert s["approve"]["token"] == VSP.lower() and s["data"][2:10] == "18cbafe5" and s["path"][0] == VSP.lower() and s["path"][-1] == WAVAX.lower()
    assert s["quote"]["slippageBps"] == 100


def test_build_validation(faked):
    with pytest.raises(HTTPException) as e:
        faked.pool_build({"side": "buy", "pay_with": "AVAX", "amount": 1, "recipient": "0x123"}, request=None)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        faked.pool_build({"side": "buy", "pay_with": "AVAX", "amount": 1, "recipient": USER, "slippage_bps": 9999}, request=None)
    with pytest.raises(HTTPException):
        faked.pool_build({"side": "buy", "pay_with": "BTC", "amount": 1, "recipient": USER}, request=None)
    with pytest.raises(HTTPException):
        faked.pool_quote(request=None, side="buy", amount=-1, pay_with="AVAX")


def test_avax_route_refused_when_venue_lacks_the_pair(faked, monkeypatch):
    monkeypatch.setattr(faked, "_wavax_pair", lambda: None)
    faked._state["routes"] = None
    assert faked.pool_routes(request=None)["routes"]["AVAX"] is False
    with pytest.raises(HTTPException) as e:
        faked.pool_quote(request=None, side="buy", amount=1.0, pay_with="AVAX")
    assert e.value.status_code == 503
    # USDC still works
    assert faked.pool_quote(request=None, side="buy", amount=10.0, pay_with="USDC")["hops"] == 1
