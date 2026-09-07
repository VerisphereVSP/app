# app/chain/registry_reader.py — read-only PostRegistry access (no wallet).
# security review 2026-09 (M4): link_post falls back to a live getClaim()
# when the indexer has not caught up yet. Deliberately does NOT import
# mm_wallet (which is being retired) — it reuses the pool reader's shared
# read-only Web3 provider.
from web3 import Web3
from .abi import POST_REGISTRY_ABI
from .pool_price import _get_w3
from config import POST_REGISTRY_ADDRESS

_registry = None


def _get_registry():
    global _registry
    if _registry is None:
        _registry = _get_w3().eth.contract(
            address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=POST_REGISTRY_ABI
        )
    return _registry


def get_claim_text(post_id: int) -> str | None:
    """On-chain claim text for a post id, or None if unreadable/empty."""
    try:
        t = _get_registry().functions.getClaim(int(post_id)).call()
        return t if t else None
    except Exception:
        return None
