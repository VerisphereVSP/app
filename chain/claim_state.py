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
from .abi import POST_REGISTRY_ABI, PROTOCOL_VIEWS_ABI

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

def _views():
    if not PROTOCOL_VIEWS_ADDRESS:
        raise ValueError("PROTOCOL_VIEWS_ADDRESS not set")
    return w3.eth.contract(
        address=Web3.to_checksum_address(PROTOCOL_VIEWS_ADDRESS),
        abi=PROTOCOL_VIEWS_ABI,
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
        views = _views()
        summary = views.functions.getClaimSummary(post_id).call()
        # ProtocolViews.ClaimSummary field order (core/src/ProtocolViews.sol):
        # 0 text, 1 supportStake, 2 challengeStake, 3 totalStake, 4 postingFee,
        # 5 isActive, 6 baseVSRay, 7 effectiveVSRay, 8 incomingCount,
        # 9 outgoingCount. The struct carries no claim id — it is the argument.
        support = int(summary[1])
        challenge = int(summary[2])
        return {
            "claim_id": post_id,
            "text": str(summary[0]),
            "eVS": _ray_to_pct(int(summary[7])),
            "stake": {"support": support, "challenge": challenge, "total": int(summary[3])},
            "links": {"incoming": int(summary[8]), "outgoing": int(summary[9])},
            "is_active": bool(summary[5]),
            "posting_fee": int(summary[4]),
        }
    except Exception as e:
        logger.error(f"fetch_claim_state({post_id}) failed: {e}")
        return {
            "claim_id": post_id, "text": "", "eVS": 0,
            "stake": {"support": 0, "challenge": 0, "total": 0},
            "links": {"incoming": 0, "outgoing": 0},
            "is_active": False, "posting_fee": 0,
        }