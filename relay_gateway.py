# app/relay_gateway.py
"""
Signing config + calldata for external wallet clients (the VeriSphere extension).

The extension holds no ABIs and no deployment artifacts. It needs two things
from us before it can sign:

  GET  /api/relay/config — the EIP-712 domains to sign against (Forwarder for
       meta-transactions, VSPToken for EIP-2612 permits), the contract
       addresses, the posting fee, and enough chain metadata to prompt
       wallet_addEthereumChain.
  POST /api/relay/build — the {to, data} for a supported action, plus the
       allowance the action will need, so the client can size its permit.

Both are read-only: nothing here signs, submits, or spends. The existing
/api/relay/async endpoint is still the only way a transaction leaves the app.

Domains are cached process-wide. They are immutable per deployment (name and
version are set at contract init), so re-reading them per request would be two
wasted RPC calls on the critical path of every wallet action.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from web3 import Web3

from config import (
    CHAIN_ID,
    FORWARDER_ADDRESS,
    POST_REGISTRY_ADDRESS,
    PROTOCOL_VIEWS_ADDRESS,
    RPC_URL_READ,
    STAKE_ENGINE_ADDRESS,
    VSP_ADDRESS,
)
from rate_limit import public_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/relay", tags=["relay"])

# Chains this deployment knows how to describe to a wallet. The rpc entry is a
# PUBLIC endpoint on purpose: it is handed to the user's wallet, so it must never
# be our own keyed RPC provider.
CHAIN_META: Dict[int, Dict[str, str]] = {
    43113: {
        "chainName": "Avalanche Fuji C-Chain",
        "explorer": "https://testnet.snowtrace.io",
        "rpc": "https://api.avax-test.network/ext/bc/C/rpc",
    },
    43114: {
        "chainName": "Avalanche C-Chain",
        "explorer": "https://snowtrace.io",
        "rpc": "https://api.avax.network/ext/bc/C/rpc",
    },
}

# ERC-5267. Both the Forwarder and VSPToken expose it via OpenZeppelin's EIP712,
# and it is all we need from either, so we declare it rather than depending on
# the build artifacts being present.
EIP712_DOMAIN_ABI = [{
    "type": "function",
    "name": "eip712Domain",
    "stateMutability": "view",
    "inputs": [],
    "outputs": [
        {"name": "fields", "type": "bytes1"},
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
        {"name": "salt", "type": "bytes32"},
        {"name": "extensions", "type": "uint256[]"},
    ],
}]

POSTING_FEE_ABI = [{
    "type": "function",
    "name": "postingFeeVSP",
    "stateMutability": "view",
    "inputs": [],
    "outputs": [{"name": "", "type": "uint256"}],
}]

SET_STAKE_ABI = [{
    "type": "function",
    "name": "setStake",
    "stateMutability": "nonpayable",
    "inputs": [{"name": "postId", "type": "uint256"}, {"name": "target", "type": "int256"}],
    "outputs": [],
}]

CREATE_CLAIM_ABI = [{
    "type": "function",
    "name": "createClaim",
    "stateMutability": "nonpayable",
    "inputs": [{"name": "text_", "type": "string"}],
    "outputs": [{"name": "postId", "type": "uint256"}],
}]

APPROVE_ABI = [{
    "type": "function",
    "name": "approve",
    "stateMutability": "nonpayable",
    "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
    "outputs": [{"type": "bool"}],
}]

MAX_UINT256 = 2**256 - 1
# patch_verity_approve_allowlist: the only legitimate spenders in the protocol.
# An encoder must never sign-ready an infinite approval to an arbitrary address.
ALLOWED_SPENDERS = {
    a.lower() for a in (STAKE_ENGINE_ADDRESS, POST_REGISTRY_ADDRESS) if a
}
WEI = 10**18
MAX_CLAIM_LENGTH = 500
# Guards the int256 encoding below and any accidental fat-finger from a client.
MAX_STAKE_VSP = 1_000_000_000


def _w3():
    from chain.provider import w3  # patch_trackb_shared_w3: decoupled from the MM module
    return w3


def _encode_call(contract, fn_name: str, args: list) -> str:
    """Calldata for a contract call, across web3.py versions.

    requirements.txt allows web3>=6, and v7 renamed Contract.encodeABI to
    encode_abi with a different keyword. Try the new name first, then the old.
    """
    new = getattr(contract, "encode_abi", None)
    if new is not None:
        try:
            return new(fn_name, args=args)
        except TypeError:
            return new(abi_element_identifier=fn_name, args=args)
    return contract.encodeABI(fn_name=fn_name, args=args)


def _read_domain(address: str) -> Tuple[str, str]:
    """(name, version) from a contract's ERC-5267 domain."""
    c = _w3().eth.contract(address=Web3.to_checksum_address(address), abi=EIP712_DOMAIN_ABI)
    d = c.functions.eip712Domain().call()
    return str(d[1]), str(d[2])


def _read_posting_fee_wei() -> int:
    """Posting fee from ProtocolPolicy, via ProtocolViews.

    Falls back to 1 VSP — the deployed default, and what the gateway this
    replaces always used. A wrong fee here only mis-sizes a permit, so a
    readable-but-stale answer beats refusing to serve the config.
    """
    if not PROTOCOL_VIEWS_ADDRESS:
        return WEI
    try:
        c = _w3().eth.contract(
            address=Web3.to_checksum_address(PROTOCOL_VIEWS_ADDRESS), abi=POSTING_FEE_ABI,
        )
        return int(c.functions.postingFeeVSP().call())
    except Exception as e:
        logger.warning("relay config: postingFeeVSP read failed, assuming 1 VSP: %s", e)
        return WEI


_config_cache: Optional[Dict[str, Any]] = None


def _relay_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if not FORWARDER_ADDRESS or not VSP_ADDRESS:
        raise HTTPException(503, "Relay not configured (missing Forwarder or VSPToken address)")
    try:
        fwd_name, fwd_version = _read_domain(FORWARDER_ADDRESS)
        tok_name, tok_version = _read_domain(VSP_ADDRESS)
    except Exception as e:
        logger.error("relay config: EIP-712 domain read failed: %s", e)
        raise HTTPException(502, "Could not read signing domains from chain")

    meta = CHAIN_META.get(CHAIN_ID, {})
    _config_cache = {
        "chainId": CHAIN_ID,
        "forwarder": {
            "address": FORWARDER_ADDRESS.lower(),
            "name": fwd_name,
            "version": fwd_version,
        },
        "token": {
            "address": VSP_ADDRESS.lower(),
            "name": tok_name,
            "version": tok_version,
        },
        "postingFeeWei": str(_read_posting_fee_wei()),
        "addresses": {
            "stakeEngine": STAKE_ENGINE_ADDRESS.lower(),
            "postRegistry": POST_REGISTRY_ADDRESS.lower(),
            "vspToken": VSP_ADDRESS.lower(),
            "forwarder": FORWARDER_ADDRESS.lower(),
        },
        # patch_relay_gas_fee_2: the extension signs its standing fee allowance to the
        # COLLECTOR (the relay wallet), not the Forwarder, and labels the quote with the knob.
        "feeCollector": _fee_collector(),
        "fee": {"basis": "gas", "markup": _fee_markup(), "quoteEndpoint": "/api/relay/estimate-fee"},
        "chain": {
            "chainId": hex(CHAIN_ID),
            "chainName": meta.get("chainName", f"Chain {CHAIN_ID}"),
            "rpcUrls": [meta.get("rpc") or RPC_URL_READ],
            "nativeCurrency": {"name": "Avalanche", "symbol": "AVAX", "decimals": 18},
            "blockExplorerUrls": [meta["explorer"]] if meta.get("explorer") else [],
        },
    }
    return _config_cache


def _fee_collector() -> str:
    """patch_relay_gas_fee_2: the relay wallet collects the fee (relay.py's permitted spender)."""
    try:
        from relay_wallet import account as _relay_account
        return (_relay_account.address or "").lower()
    except Exception:
        return ""


def _fee_markup() -> float:
    try:
        from config import RELAY_FEE_MARKUP
        return float(RELAY_FEE_MARKUP)
    except Exception:
        return 0.25


def invalidate_relay_config() -> None:
    """Drop the cached config. Call after a redeploy changes addresses."""
    global _config_cache
    _config_cache = None


@router.get("/config")
@public_endpoint("/api/relay/config", cost_tier="cheap")
def relay_config(request: Request) -> Dict[str, Any]:
    """EIP-712 domains, addresses, posting fee and chain params for signing."""
    return _relay_config()


@router.post("/build")
@public_endpoint("/api/relay/build", cost_tier="cheap")
def relay_build(body: dict, request: Request) -> Dict[str, Any]:
    """Encode the calldata for a supported write action.

    Body: {"action": "setStake"|"createClaim"|"approve", ...params}
      setStake:    postId (int), targetVsp (float; negative = challenge, 0 = exit)
      createClaim: text (str)
      approve:     spender (address)

    Returns {to, data, permitValueWei} — permitValueWei is the allowance the
    action consumes, which the client uses as its permit value.
    """
    action = str(body.get("action") or "")
    w3 = _w3()

    if action == "approve":
        spender = body.get("spender")
        if not isinstance(spender, str) or not Web3.is_address(spender):
            raise HTTPException(400, "approve requires a valid 'spender' address")
        if spender.lower() not in ALLOWED_SPENDERS:  # patch_verity_approve_allowlist
            raise HTTPException(400, "spender must be a protocol contract (StakeEngine or PostRegistry)")
        token = w3.eth.contract(address=Web3.to_checksum_address(VSP_ADDRESS), abi=APPROVE_ABI)
        data = _encode_call(token, "approve", [Web3.to_checksum_address(spender), MAX_UINT256])
        return {"to": VSP_ADDRESS.lower(), "data": data, "permitValueWei": "0"}

    if action == "setStake":
        try:
            post_id = int(body["postId"])
            target_vsp = float(body["targetVsp"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "setStake requires integer 'postId' and numeric 'targetVsp'")
        if post_id < 0:
            raise HTTPException(400, "postId must be non-negative")
        if abs(target_vsp) > MAX_STAKE_VSP:
            raise HTTPException(400, f"targetVsp exceeds {MAX_STAKE_VSP}")
        abs_wei = int(round(abs(target_vsp) * WEI))
        target_wei = abs_wei if target_vsp >= 0 else -abs_wei
        engine = w3.eth.contract(
            address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS), abi=SET_STAKE_ABI,
        )
        data = _encode_call(engine, "setStake", [post_id, target_wei])
        # BOTH sides escrow VSP (setStake transferFroms for support AND
        # challenge), so any nonzero target needs allowance for |target|. Only a
        # full withdraw (target 0) moves no tokens in.
        return {
            "to": STAKE_ENGINE_ADDRESS.lower(),
            "data": data,
            "permitValueWei": str(abs_wei) if target_wei != 0 else "0",
        }

    if action == "createClaim":
        text = body.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise HTTPException(400, "createClaim requires a non-empty 'text'")
        if len(text) > MAX_CLAIM_LENGTH:
            raise HTTPException(400, f"Text exceeds {MAX_CLAIM_LENGTH} characters")
        registry = w3.eth.contract(
            address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=CREATE_CLAIM_ABI,
        )
        data = _encode_call(registry, "createClaim", [text])
        # The permit must cover the posting fee the registry pulls on create.
        return {
            "to": POST_REGISTRY_ADDRESS.lower(),
            "data": data,
            "permitValueWei": _relay_config()["postingFeeWei"],
        }

    raise HTTPException(400, f"Unsupported action: {action or '(missing)'}")
