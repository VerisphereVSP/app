"""chain/claim_summary.py — ProtocolViews.getClaimSummary decoded from raw return data.

Regression for /api/claims/{id}/live reporting effective_vs = 0 on a freshly staked
claim: core v17 (patch_game_b) dropped baseVSRay from ClaimSummary (10 -> 9 fields)
and the app kept reading the score at its old position, so it got incomingCount.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from eth_abi import encode  # noqa: E402
from web3 import Web3  # noqa: E402

from chain.claim_summary import SELECTOR, decode_claim_summary, encode_call  # noqa: E402

# eth_call ProtocolViews(0x0B7F892FfAC2D98dD0265Ec64d13B4dA4502A48F).getClaimSummary(112)
# on Fuji, 2026-10-06 — the deployed 9-field struct. 2 VSP challenged, no links, score -100.
FUJI_POST_112 = bytes.fromhex(
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000120"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000001bc16d674ec80000"
    "0000000000000000000000000000000000000000000000001bc16d674ec80000"
    "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "fffffffffffffffffffffffffffffffffffffffffffffffff21f494c589c0000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000004a"
    "434f5649442d31392076616363696e65732061726520776964656c7920637265"
    "646974656420666f72207265647563696e672074686520737072656164206f66"
    "20434f5649442d31392e00000000000000000000000000000000000000000000"
)
COVID_TEXT = "COVID-19 vaccines are widely credited for reducing the spread of COVID-19."

NINE = "(string,uint256,uint256,uint256,uint256,bool,int256,uint256,uint256)"
TEN = "(string,uint256,uint256,uint256,uint256,bool,int256,int256,uint256,uint256)"


def test_decodes_the_deployed_nine_field_layout():
    s = decode_claim_summary(FUJI_POST_112)
    assert s["fields"] == 9
    assert s["text"] == COVID_TEXT
    assert s["supportStake"] == 0
    assert s["challengeStake"] == 2 * 10**18
    assert s["totalStake"] == 2 * 10**18
    assert s["postingFee"] == 10**18
    assert s["isActive"] is True
    assert s["effectiveVSRay"] == -(10**18)  # -100%
    assert s["incomingCount"] == 0
    assert s["outgoingCount"] == 0
    assert "baseVSRay" not in s


def test_nine_field_decode_agrees_with_the_abi_encoder():
    raw = encode([NINE], [("x", 1, 2, 3, 10**18, True, -(10**18), 4, 5)])
    s = decode_claim_summary(raw)
    assert (s["supportStake"], s["challengeStake"], s["totalStake"], s["postingFee"]) == (1, 2, 3, 10**18)
    assert (s["effectiveVSRay"], s["incomingCount"], s["outgoingCount"]) == (-(10**18), 4, 5)
    assert s["text"] == "x"


def test_decodes_the_legacy_ten_field_layout():
    raw = encode([TEN], [("old claim", 3 * 10**18, 10**18, 4 * 10**18, 10**18, True, 5 * 10**17, -25 * 10**16, 2, 1)])
    s = decode_claim_summary(raw)
    assert s["fields"] == 10
    assert s["baseVSRay"] == 5 * 10**17
    assert s["effectiveVSRay"] == -25 * 10**16
    assert (s["incomingCount"], s["outgoingCount"]) == (2, 1)
    assert s["text"] == "old claim"


def test_long_text_spanning_several_words():
    text = "y" * 150
    s = decode_claim_summary(encode([NINE], [(text, 0, 0, 0, 0, False, 0, 0, 0)]))
    assert s["text"] == text and s["isActive"] is False


def test_unknown_layout_raises():
    with pytest.raises(ValueError):
        decode_claim_summary(encode(["(string,uint256,uint256)"], [("x", 1, 2)]))


def test_truncated_data_raises():
    with pytest.raises(ValueError):
        decode_claim_summary(FUJI_POST_112[:200])


def test_encode_call():
    data = encode_call(112)
    assert len(data) == 36
    assert data[:4] == SELECTOR == Web3.keccak(text="getClaimSummary(uint256)")[:4]
    assert int.from_bytes(data[4:], "big") == 112


def test_live_endpoint_reports_the_chain_score(monkeypatch):
    import claim_views

    monkeypatch.setattr(claim_views, "PROTOCOL_VIEWS_ADDRESS", "0x0B7F892FfAC2D98dD0265Ec64d13B4dA4502A48F")
    monkeypatch.setattr(claim_views, "read_claim_summary", lambda w3, addr, pid: decode_claim_summary(FUJI_POST_112))
    r = claim_views.claim_live(112)
    assert r["effective_vs"] == -100.0
    assert "base_vs" not in r  # one score since core v17; the old alias is gone
    assert r["active"] is True
    assert (r["support_vsp"], r["challenge_vsp"], r["total_vsp"], r["posting_fee_vsp"]) == (0.0, 2.0, 2.0, 1.0)


def test_live_endpoint_reads_effective_not_base_on_the_legacy_layout(monkeypatch):
    import claim_views

    raw = encode([TEN], [("old", 3 * 10**18, 10**18, 4 * 10**18, 10**18, True, 5 * 10**17, -25 * 10**16, 0, 0)])
    monkeypatch.setattr(claim_views, "PROTOCOL_VIEWS_ADDRESS", "0xB15f16Bb65E7aE994F1aAC56Ff2Ea71E53CF92d6")
    monkeypatch.setattr(claim_views, "read_claim_summary", lambda w3, addr, pid: decode_claim_summary(raw))
    r = claim_views.claim_live(1)
    assert r["effective_vs"] == -25.0
    assert "base_vs" not in r


def test_fetch_claim_state_reads_score_and_links_from_the_right_fields(monkeypatch):
    import chain.claim_state as cs

    monkeypatch.setattr(cs, "PROTOCOL_VIEWS_ADDRESS", "0x0B7F892FfAC2D98dD0265Ec64d13B4dA4502A48F")
    monkeypatch.setattr(cs, "read_claim_summary", lambda w3, addr, pid: decode_claim_summary(FUJI_POST_112))
    st = cs.fetch_claim_state(112)
    assert st["eVS"] == -100.0
    assert st["is_active"] is True
    assert st["links"] == {"incoming": 0, "outgoing": 0}
    assert st["stake"] == {"support": 0, "challenge": 2 * 10**18, "total": 2 * 10**18}
    assert st["text"] == COVID_TEXT
    assert st["posting_fee"] == 10**18
