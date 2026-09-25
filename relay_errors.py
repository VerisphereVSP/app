"""relay_errors.py — revert-selector -> user message table and decoder.

Kept free of framework/RPC imports so it can be unit-tested on the dev host
(relay.py asserts RPC connectivity at import). patch_game_b.
"""
import re

_KNOWN_ERRORS = {
    "cbca5aa2": "Amount cannot be zero",
    "0dfa289a": "Invalid side (must be 0 or 1)",
    "f0a42d4c": "Not enough stake to withdraw",
    "546dcceb": "Cannot stake on opposite side — you already have a stake on the other side of this claim",
    "33cb1ab6": "Post is not active",
    "7e81c055": "Invalid post ID",
    "b00d4d75": "This link already exists",
    "c314bc02": "Claim already exists",
    "49b39990": "Cannot link a claim to itself",
    "7ad1f845": "Source post does not exist",
    "22fa5e05": "Target post does not exist",
    "7861979c": "Source post must be a claim",
    "2b3d067e": "Target post must be a claim",
    "bd73f403": "Claim text is too long",
    "fb8f41b2": "Insufficient VSP allowance",
    "e450d38c": "Insufficient VSP balance",
    "d6bda275": "Transaction failed — likely insufficient balance or contract rejection",
    # patch_game_b (whitepaper v17 §3.2): oversized inline settlement is deferred to the keeper
    "1e6049a5": "This post is being settled by the network — please retry in a moment",
    "609b0047": "This post's evidence graph could not be scored exactly — please retry in a moment",
    # kill-switch drill 2026-09-24: a pause must read as a pause, not as a gas error
    "fb8e4881": "The protocol is paused by the guardian — staking and posting are temporarily disabled",
}


def _decode_revert_reason(err) -> str:
    """Extract a human-readable revert reason from a web3 call exception."""
    s = str(err)
    m = re.search(r"0x([0-9a-fA-F]{8})", s)
    if m:
        sel = m.group(1).lower()
        if sel in _KNOWN_ERRORS:
            return _KNOWN_ERRORS[sel]
    if "0x11" in s or "underflow" in s.lower():
        return "Contract arithmetic error (this is a bug — please report)"
    m2 = re.search(r"execution reverted: ?([^\n,\"]+)", s)
    if m2:
        return f"Reverted: {m2.group(1).strip()}"
    return "Transaction would fail on-chain. Common causes: insufficient VSP balance, duplicate action, or contract constraint."


