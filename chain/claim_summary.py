# app/chain/claim_summary.py
"""ProtocolViews.getClaimSummary, decoded from the raw return data.

Why not the contract ABI: core v17 (patch_game_b) dropped `baseVSRay` from the
ClaimSummary struct (10 fields -> 9), and the ABI artifact under core/out is
whatever was last built, not necessarily what is deployed. web3 decodes a
9-field return with a 10-field ABI without complaint: every word after
`isActive` shifts by one, so the score lands in `baseVSRay` and `incomingCount`
lands in `effectiveVSRay`. That is how /api/claims/{id}/live came to report
effective_vs = 0 on a freshly staked claim.

The fields the app needs are fixed-width words whose positions depend only on
the struct length, and the struct length is in the return data itself (the
`text` head offset is 32 * field count). So decode positionally against the
layout the chain reports, and the result is right for both deployments.
"""
from __future__ import annotations

from typing import Any, Dict

from web3 import Web3

SELECTOR = Web3.keccak(text="getClaimSummary(uint256)")[:4]

# Word position of each field by struct length. `text` (field 0) is dynamic and
# handled separately; everything else is one 32-byte word.
_LAYOUTS: Dict[int, Dict[str, int]] = {
    # core >= v17: a post has ONE score, baseVSRay is internal.
    9: {
        "supportStake": 1, "challengeStake": 2, "totalStake": 3, "postingFee": 4,
        "isActive": 5, "effectiveVSRay": 6, "incomingCount": 7, "outgoingCount": 8,
    },
    # Older deployments (e.g. the 2026-07-15 Fuji redeploy) still expose baseVSRay.
    10: {
        "supportStake": 1, "challengeStake": 2, "totalStake": 3, "postingFee": 4,
        "isActive": 5, "baseVSRay": 6, "effectiveVSRay": 7, "incomingCount": 8, "outgoingCount": 9,
    },
}
_SIGNED = {"baseVSRay", "effectiveVSRay"}


def encode_call(post_id: int) -> bytes:
    """Calldata for getClaimSummary(post_id)."""
    return SELECTOR + int(post_id).to_bytes(32, "big")


def decode_claim_summary(raw: bytes) -> Dict[str, Any]:
    """Decode an ABI-encoded ClaimSummary. Raises ValueError on an unknown layout.

    Returns ints for the stake/fee/count fields (wei and counts), a bool for
    `isActive`, the claim `text`, and `fields` (the struct length seen).
    `baseVSRay` is present only for the 10-field layout.
    """
    if len(raw) < 64 or len(raw) % 32:
        raise ValueError(f"malformed ClaimSummary return data ({len(raw)} bytes)")

    def word(i: int) -> bytes:
        w = raw[i * 32:(i + 1) * 32]
        if len(w) != 32:
            raise ValueError(f"ClaimSummary return data truncated at word {i}")
        return w

    # A struct with a dynamic member is returned as one dynamic tuple: word 0
    # is the offset of the tuple, and inside it the `text` head word is the
    # offset of the string relative to the tuple start, i.e. 32 * field count.
    tuple_start = int.from_bytes(word(0), "big") // 32
    n_fields = int.from_bytes(word(tuple_start), "big") // 32
    layout = _LAYOUTS.get(n_fields)
    if layout is None:
        raise ValueError(f"unknown ClaimSummary layout ({n_fields} fields)")

    out: Dict[str, Any] = {"fields": n_fields}
    for name, pos in layout.items():
        out[name] = int.from_bytes(word(tuple_start + pos), "big", signed=name in _SIGNED)
    out["isActive"] = bool(out["isActive"])

    text_len_word = tuple_start + n_fields
    text_len = int.from_bytes(word(text_len_word), "big")
    start = (text_len_word + 1) * 32
    if start + text_len > len(raw):
        raise ValueError("ClaimSummary text truncated")
    out["text"] = raw[start:start + text_len].decode("utf-8", errors="replace")
    return out


def read_claim_summary(w3, views_address: str, post_id: int) -> Dict[str, Any]:
    """One eth_call to ProtocolViews.getClaimSummary, decoded by the chain's layout.

    Raises whatever the RPC raises (including the contract's "not claim" revert).
    """
    raw = w3.eth.call({
        "to": Web3.to_checksum_address(views_address),
        "data": encode_call(post_id),
    })
    return decode_claim_summary(bytes(raw))
