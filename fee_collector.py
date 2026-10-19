# app/fee_collector.py — patch_relay_gas_fee
"""
Collect the relay's gas-indexed fee AFTER the user's action has confirmed.

The relay quotes the fee at submission (relay.py) and records it on the tx_log
row (fee_charge_vsp, fee_status='pending'). The worker's indexer poll loop
calls collect_due_fees() right after resolve_pending_txs(): for every row that
is now 'confirmed' it sends VSPToken.transferFrom(user -> TREASURY, fee) from
the relay wallet, which the user has authorised with a standing EIP-2612
allowance (the "collector" allowance signed in the client). Reverted or dropped
actions are waived — the user never pays for a failure.

No contract carries any fee knowledge: the amount is the relay's own quote.
patch_fee_collector: with FEE_COLLECTOR_ADDRESS set, the relay calls
FeeCollector.collect(user, amount, ref) — the user's allowance names that
verified contract, which caps every call (5 VSP) and every user per day
(25 VSP) and can be paused by the Guardian. A user whose allowance still names
the relay wallet (permitted before the switch) is collected the legacy way
(transferFrom) until their next action re-permits the contract; clients learn
the new spender from /relay/config and /relay/fee-policy, and a permit naming
the relay wallet from a not-yet-reloaded client is still accepted meanwhile.

Failure policy: a collection that reverts (allowance revoked, balance gone)
counts as an attempt; after MAX_ATTEMPTS the row is 'failed' and left for the
operator (dashboard: fee_status). Nonce collisions with the app process (which
sends from the same relay wallet) surface as send errors and are simply
retried on the next poll.
"""
import logging
import os

from web3 import Web3

from tx_log import get_fees_due, waive_fees_for_failed, mark_fee_collected, mark_fee_attempt

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = int(os.getenv("RELAY_FEE_COLLECT_MAX_ATTEMPTS", "5"))
BATCH = int(os.getenv("RELAY_FEE_COLLECT_BATCH", "10"))

_TRANSFER_FROM_ABI = [{
    "inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
               {"name": "value", "type": "uint256"}],
    "name": "transferFrom", "outputs": [{"type": "bool"}],
    "stateMutability": "nonpayable", "type": "function",
}, {
    "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
    "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function",
}]
# patch_fee_collector: core/src/FeeCollector.sol
_FEE_COLLECTOR_ABI = [{
    "inputs": [{"name": "user", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "ref", "type": "bytes32"}],
    "name": "collect", "outputs": [], "stateMutability": "nonpayable", "type": "function",
}, {
    "inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function",
}]


def collect_route(fee_collector: str, allowance_contract: int, allowance_legacy: int, amount_wei: int) -> str:
    """Pure: which path collects this fee. 'contract' when the FeeCollector is deployed and the user's allowance
    to it covers the fee; 'legacy' (relay-wallet transferFrom) only while the contract is unset, or for a user
    whose allowance still names the relay wallet and not yet the contract; 'none' when neither can pay (the
    attempt is still made on the preferred path so the revert is recorded)."""
    if not fee_collector:
        return "legacy"
    if allowance_contract >= amount_wei:
        return "contract"
    if allowance_legacy >= amount_wei:
        return "legacy"
    return "contract"


def _ref_bytes32(tx_hash: str) -> bytes:
    """The relayed action's tx hash as the FeeCollected ref (zero-padded if shorter)."""
    h = (tx_hash or "").lower().removeprefix("0x")
    try:
        b = bytes.fromhex(h)
    except ValueError:
        b = b""
    return b[:32].rjust(32, b"\0")


def _wallet():
    """The relay wallet (KMS-backed, patch_kms_relay) — imported lazily so the
    worker only touches the signer when there is a fee to collect."""
    from relay_wallet import w3, account, sign_and_send, TxRevertedError
    return w3, account, sign_and_send, TxRevertedError


def _send_collect(user: str, amount_wei: int, treasury: str, token: str, wallet=None, tx_hash: str = "") -> str:
    w3, account, sign_and_send, _ = wallet or _wallet()
    from config import RELAY_FEE_COLLECT_GAS, RELAY_FEE_COLLECT_GAS_CONTRACT, FEE_COLLECTOR_ADDRESS
    tok = w3.eth.contract(address=Web3.to_checksum_address(token), abi=_TRANSFER_FROM_ABI)
    u = Web3.to_checksum_address(user)
    route = "legacy"
    if FEE_COLLECTOR_ADDRESS:
        fc_addr = Web3.to_checksum_address(FEE_COLLECTOR_ADDRESS)
        a_new = tok.functions.allowance(u, fc_addr).call()
        a_old = tok.functions.allowance(u, account.address).call()
        route = collect_route(FEE_COLLECTOR_ADDRESS, a_new, a_old, int(amount_wei))
    if route == "contract":
        fc = w3.eth.contract(address=fc_addr, abi=_FEE_COLLECTOR_ABI)
        tx = fc.functions.collect(u, int(amount_wei), _ref_bytes32(tx_hash)).build_transaction(
            {"from": account.address, "gas": int(RELAY_FEE_COLLECT_GAS_CONTRACT)})
        logger.info("fee collect via FeeCollector %s user=%s", fc_addr, user[:10])
    else:
        tx = tok.functions.transferFrom(u, Web3.to_checksum_address(treasury), int(amount_wei)).build_transaction(
            {"from": account.address, "gas": int(RELAY_FEE_COLLECT_GAS)})
        if FEE_COLLECTOR_ADDRESS:
            logger.info("fee collect legacy transferFrom (allowance not yet on the FeeCollector) user=%s", user[:10])
    return sign_and_send(tx)


def collect_due_fees(db, *, send=None, limit: int = BATCH) -> dict:
    """One pass. `send` is injectable for tests: send(user, amount_wei) -> tx hash.
    Returns counts {waived, collected, failed, retried}."""
    from config import RELAY_FEE_ENABLED, TREASURY_ADDRESS, VSP_TOKEN_ADDRESS
    out = {"waived": 0, "collected": 0, "failed": 0, "retried": 0}
    try:
        out["waived"] = waive_fees_for_failed(db)
        db.commit()
    except Exception as e:
        logger.warning("fee_collector: waive sweep failed: %s", e)
        db.rollback()
    if not RELAY_FEE_ENABLED:
        return out
    rows = get_fees_due(db, limit=limit, max_attempts=MAX_ATTEMPTS)
    if not rows:
        return out
    wallet = None
    for row in rows:
        amount_wei = int(round(float(row.fee_charge_vsp) * 1e18))
        try:
            if send is not None:
                fee_tx = send(row.user_address, amount_wei)
            else:
                wallet = wallet or _wallet()
                fee_tx = _send_collect(row.user_address, amount_wei, TREASURY_ADDRESS, VSP_TOKEN_ADDRESS, wallet,
                                       tx_hash=getattr(row, "tx_hash", "") or "")
            mark_fee_collected(db, row.id, fee_tx, float(row.fee_charge_vsp))
            db.commit()
            out["collected"] += 1
            logger.info("fee collected: tx_log %d user=%s fee=%.6f VSP tx=%s",
                        row.id, row.user_address[:10], float(row.fee_charge_vsp), fee_tx)
        except Exception as e:
            db.rollback()
            # a revert (allowance revoked / balance gone) is final after MAX_ATTEMPTS; a
            # transport or nonce error is retried next pass
            final = (int(row.fee_attempts) + 1) >= MAX_ATTEMPTS
            try:
                mark_fee_attempt(db, row.id, f"{type(e).__name__}: {e}", final)
                db.commit()
            except Exception as e2:
                logger.warning("fee_collector: could not record attempt for %d: %s", row.id, e2)
                db.rollback()
            out["failed" if final else "retried"] += 1
            logger.warning("fee collection %s: tx_log %d user=%s fee=%.6f: %s",
                           "FAILED (final)" if final else "retry", row.id, row.user_address[:10],
                           float(row.fee_charge_vsp), e)
    return out
