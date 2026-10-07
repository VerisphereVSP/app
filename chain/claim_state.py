# app/chain/claim_state.py
"""
Read-only blockchain queries for claim state.
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from web3 import Web3

from mm_wallet import w3
from config import POST_REGISTRY_ADDRESS, PROTOCOL_VIEWS_ADDRESS
from .abi import POST_REGISTRY_ABI
from .claim_summary import read_claim_summary

logger = logging.getLogger(__name__)

_RAY = 10 ** 18

def _ray_to_pct(ray_value: int) -> float:
    """Convert effectiveVSRay (1e18) to percentage [-100, 100].
    patch_vs_single_source: delegates to chain.vs (single conversion)."""
    from chain.vs import ray_to_pct
    return round(ray_to_pct(ray_value), 2)

def _registry():
    return w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=POST_REGISTRY_ABI,
    )

def find_claim_by_text(text: str) -> Optional[int]:
    """Find post ID by exact claim text match. Returns None if not found."""
    try:
        registry = _registry()
        next_id: int = registry.functions.nextPostId().call()
        normalized = text.strip()

        for post_id in range(next_id):
            try:
                post = registry.functions.getPost(post_id).call()
                if post[2] != 0:  # contentType: 0=Claim, 1=Link
                    continue
                content_id = post[3]
                claim_text: str = registry.functions.getClaim(content_id).call()
                if claim_text.strip() == normalized:
                    return post_id
            except Exception:
                continue
    except Exception as e:
        logger.error(f"find_claim_by_text failed: {e}")
    return None

def fetch_claim_state(post_id: int) -> Dict[str, Any]:
    """Fetch full claim state from ProtocolViews. Never raises."""
    try:
        if not PROTOCOL_VIEWS_ADDRESS:
            raise ValueError("PROTOCOL_VIEWS_ADDRESS not set")
        # Decoded by the field count the chain reports (9 since core v17, 10
        # before), so a stale ABI artifact can't shift the score into the wrong
        # field — see chain/claim_summary.py. The struct carries no claim id —
        # it is the argument.
        s = read_claim_summary(w3, PROTOCOL_VIEWS_ADDRESS, post_id)
        return {
            "claim_id": post_id,
            "text": s["text"],
            "eVS": _ray_to_pct(s["effectiveVSRay"]),
            "stake": {"support": s["supportStake"], "challenge": s["challengeStake"], "total": s["totalStake"]},
            "links": {"incoming": s["incomingCount"], "outgoing": s["outgoingCount"]},
            "is_active": s["isActive"],
            "posting_fee": s["postingFee"],
        }
    except Exception as e:
        logger.error(f"fetch_claim_state({post_id}) failed: {e}")
        return {
            "claim_id": post_id, "text": "", "eVS": 0,
            "stake": {"support": 0, "challenge": 0, "total": 0},
            "links": {"incoming": 0, "outgoing": 0},
            "is_active": False, "posting_fee": 0,
        }