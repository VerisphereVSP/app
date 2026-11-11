#!/usr/bin/env python3
"""app/tx_signer.py — patch_bundle11_tx_signer_dedup

Shared, address-agnostic transaction signer. Extracts the signing skeleton that
was duplicated between mm_wallet.py and treasury_wallet.py into one place, with
the two genuine behavioral differences parameterized so each caller's behavior
is preserved byte-for-byte:

  * receipt_timeout        : mm_wallet=60s, treasury_wallet=120s
  * gas_estimate_fallback  : mm_wallet=250_000 (guess + broadcast on estimate
                             failure); treasury_wallet=None (raise TxRevertedError
                             and do NOT broadcast a doomed tx, e.g. mint-over-cap)

mm_wallet.py and treasury_wallet.py are now thin shims that build their own
account (their own key + address assertion) and w3, then bind a sign_and_send via
make_sign_and_send(). Both re-export TxRevertedError (now a single shared class).

web3 is imported lazily (only inside build_w3 and inside the signer's TimeExhausted
handling) so importing this module pulls no heavy deps — matching the original
modules' inline `from web3.exceptions import TimeExhausted`.
"""
import logging
import os

_module_logger = logging.getLogger(__name__)

# patch_tx_fee_headroom: a transaction priced at exactly the last block's base fee stops
# being includable the moment the base fee ticks up, and the node drops it after a while
# (the prod keepers lost every send on 2026-11-11 that way: maxFeePerGas = baseFee + 1.5x a
# 150-wei priority suggestion). Price the EIP-1559 way — twice the base fee of headroom plus
# a priority floor — and let callers that must know a send landed treat a receipt timeout as
# the failure it is (on_timeout="raise").
TX_BASE_FEE_HEADROOM = 2
TX_PRIORITY_FLOOR_WEI = int(float(os.getenv("TX_PRIORITY_FLOOR_GWEI", "1")) * 1_000_000_000)


class TxTimeoutError(Exception):
    """patch_tx_fee_headroom: raised by sign_and_send(on_timeout="raise") when no receipt
    arrived within receipt_timeout. The tx may still land later; callers re-check chain
    state before resending. Carries tx_hash like TxRevertedError."""

    def __init__(self, tx_hash: str, timeout: float = 0):
        self.tx_hash = tx_hash
        self.timeout = timeout
        super().__init__(f"no receipt within {timeout:g}s (tx_hash={tx_hash})")


class TxRevertedError(Exception):
    """Raised by sign_and_send when wait_for_transaction_receipt returns
    status==0 (on-chain revert), or (for the no-fallback gas policy) when gas
    estimation reverts pre-broadcast. Carries tx_hash so callers that need to
    record the submitted hash (e.g. /api/relay/async tx_log row, or the treasury
    worker's alert) can do so.
    """

    def __init__(self, tx_hash: str, receipt=None, message: str = ""):
        self.tx_hash = tx_hash
        self.receipt = receipt
        super().__init__(message or f"on-chain revert (tx_hash={tx_hash})")


_FAILOVER_PROVIDER_CLASS = None


def _failover_provider_class():
    """Lazily define FailoverHTTPProvider (keeps web3 import lazy, like the rest of
    this module). Cached after first use. patch_bundle10_rpc_failover."""
    global _FAILOVER_PROVIDER_CLASS
    if _FAILOVER_PROVIDER_CLASS is not None:
        return _FAILOVER_PROVIDER_CLASS
    from web3.providers import JSONBaseProvider
    from web3 import HTTPProvider

    class FailoverHTTPProvider(JSONBaseProvider):
        """HTTP provider over an ordered list of endpoints. Each JSON-RPC request is
        tried in order; a TRANSPORT failure (connection refused / timeout / 5xx / 429
        -> exception from the child provider) fails over to the next endpoint. A
        JSON-RPC *application* error (e.g. execution reverted) is a valid response and
        returned as-is -- never a failover trigger. Sticky on the last good endpoint.
        Re-broadcasting the same signed raw tx to a fallback is idempotent (same
        hash/nonce), so failover is safe for writes too."""

        def __init__(self, endpoint_uris, request_kwargs=None, logger=None):
            super().__init__()
            uris = [u for u in endpoint_uris if u]
            if not uris:
                raise ValueError("FailoverHTTPProvider: no endpoint URIs")
            self._uris = uris
            self._providers = [HTTPProvider(u, request_kwargs=request_kwargs) for u in uris]
            self._active = 0
            self._log = logger or logging.getLogger("tx_signer.failover")

        @staticmethod
        def _redact(uri):
            if "/v2/" in uri:
                return uri.split("/v2/")[0] + "/v2/***"
            return uri.split("?")[0]

        def make_request(self, method, params):
            n = len(self._providers)
            order = list(range(self._active, n)) + list(range(0, self._active))
            last_exc = None
            for idx in order:
                try:
                    resp = self._providers[idx].make_request(method, params)
                except Exception as e:  # transport failure -> try the next endpoint
                    last_exc = e
                    self._log.warning("RPC endpoint #%d (%s) failed on %s: %s",
                                      idx, self._redact(self._uris[idx]), method, e)
                    continue
                if idx != self._active:
                    self._log.warning("RPC failover: now using endpoint #%d (%s)",
                                      idx, self._redact(self._uris[idx]))
                    self._active = idx
                return resp
            raise last_exc if last_exc is not None else RuntimeError(
                "FailoverHTTPProvider: all endpoints failed")

        def is_connected(self, show_traceback=False):
            for p in self._providers:
                try:
                    if p.is_connected():
                        return True
                except Exception:
                    continue
            return False

    _FAILOVER_PROVIDER_CLASS = FailoverHTTPProvider
    return _FAILOVER_PROVIDER_CLASS


def build_w3(rpc_urls, *, conn_err_msg="Web3 RPC not connected", require_connected=True):
    """Construct a Web3 client over one or more RPC endpoints and inject Avalanche POA
    middleware. `rpc_urls` may be a single URL string (behavior unchanged) or an ordered
    list [primary, fallback, ...]; 2+ endpoints use a transport-level failover provider.
    By default asserts connectivity (pass require_connected=False for lazy read paths
    that should not probe at construction). patch_bundle10_rpc_failover."""
    from web3 import Web3

    if isinstance(rpc_urls, str):
        urls = [rpc_urls]
    else:
        urls = [u for u in rpc_urls if u]
    if not urls:
        raise RuntimeError(conn_err_msg + " (no RPC endpoints configured)")

    if len(urls) == 1:
        provider = Web3.HTTPProvider(urls[0])  # single endpoint: unchanged behavior
    else:
        provider = _failover_provider_class()(urls)
    w3 = Web3(provider)

    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except ImportError:
        from web3.middleware import geth_poa_middleware
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)

    if require_connected and not w3.is_connected():
        raise RuntimeError(conn_err_msg)
    return w3


def make_sign_and_send(*, account, w3, receipt_timeout, gas_estimate_fallback,
                       logger=None, label="sign_and_send", on_timeout="return"):
    """Return a sign_and_send(tx) bound to `account` / `w3` with the given receipt
    timeout and gas-estimate-failure policy.

      gas_estimate_fallback:
        - int  -> on estimate_gas failure, set tx['gas']=fallback and broadcast
                  (mm_wallet behavior: 250_000)
        - None -> on estimate_gas failure, raise TxRevertedError and do NOT
                  broadcast (treasury_wallet behavior)
      on_timeout (patch_tx_fee_headroom):
        - "return" -> no receipt within receipt_timeout: warn and return the hash
                      (relay / mm / treasury: a watcher resolves pending hashes)
        - "raise"  -> raise TxTimeoutError(hash) instead (keepers: a send that did
                      not land is a failure to retry, never a success to record)
    """
    if on_timeout not in ("return", "raise"):
        raise ValueError(f"on_timeout must be 'return' or 'raise', got {on_timeout!r}")
    log = logger or _module_logger

    def sign_and_send(tx: dict) -> str:
        tx = dict(tx)
        tx.pop("gasPrice", None)

        # EIP-1559 fee fields, with a legacy gasPrice fallback if the node
        # doesn't surface a base fee / priority fee.
        try:
            base_fee = w3.eth.get_block("latest").baseFeePerGas
            # patch_tx_fee_headroom: priority floored, ceiling = 2x base + priority
            priority = max(w3.eth.max_priority_fee * 150 // 100, TX_PRIORITY_FLOOR_WEI)
            tx["type"] = 2
            tx["maxFeePerGas"] = TX_BASE_FEE_HEADROOM * base_fee + priority
            tx["maxPriorityFeePerGas"] = priority
        except Exception:
            gp = w3.eth.gas_price
            tx["gasPrice"] = max(gp * 120 // 100, gp + TX_PRIORITY_FLOOR_WEI)  # patch_tx_fee_headroom

        tx["nonce"] = w3.eth.get_transaction_count(account.address, "pending")
        tx["chainId"] = w3.eth.chain_id

        if "gas" not in tx:
            try:
                tx["gas"] = w3.eth.estimate_gas(tx)
            except Exception:
                if gas_estimate_fallback is not None:
                    # mm_wallet: estimate failed (often a transient RPC issue);
                    # broadcast with a conservative default gas limit.
                    tx["gas"] = gas_estimate_fallback
                else:
                    # treasury_wallet: estimate_gas reverts if the call itself
                    # would revert (e.g. mint over cap). Surface that as a revert
                    # rather than guessing a gas limit and broadcasting a doomed tx.
                    raise TxRevertedError(
                        "0x0",
                        message="gas estimation reverted (call would revert pre-broadcast)",
                    )

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        # Wait for the receipt before returning so the caller's next tx sees the
        # updated chain state (avoids races on stake flips: withdraw old side then
        # stake new side).
        #   status == 1  -> success: return the canonical hash.
        #   status == 0  -> on-chain revert: raise TxRevertedError carrying the hash.
        #   timeout      -> caller may still want the hash (relay records it and the
        #                   resolve_pending_txs watcher catches up); warn, don't raise.
        from web3.exceptions import TimeExhausted
        _raw = tx_hash.hex().lower().removeprefix("0x")
        _canonical = "0x" + _raw
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=receipt_timeout)
        except TimeExhausted:
            if on_timeout == "raise":  # patch_tx_fee_headroom
                log.warning("%s: no receipt for %s within %ss — not counted as landed",
                            label, _canonical, receipt_timeout)
                raise TxTimeoutError(_canonical, receipt_timeout)
            log.warning(
                "%s: receipt timeout for %s; returning hash anyway",
                label, _canonical,
            )
            return _canonical
        if getattr(receipt, "status", 1) == 0:
            raise TxRevertedError(_canonical, receipt=receipt)
        return _canonical

    return sign_and_send
