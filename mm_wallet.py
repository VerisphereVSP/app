# app/mm_wallet.py
# patch_bundle11_tx_signer_dedup: thin shim over the shared signer in tx_signer.py.
# Preserves the public API used across the app (w3, account, sign_and_send,
# TxRevertedError) with byte-for-byte-identical behavior: receipt timeout 60s and
# the 250_000 gas fallback (broadcast on estimate failure).
# patch_kms_mm: the MM key lives in GCP Cloud KMS (HSM); the private key never
# exists on this box. Fail-loud: MM_KMS_KEY + MM_ADDRESS required and must agree.
# MM_PRIVATE_KEY is deliberately ignored.
# patch_trackb_shared_w3: RPC list + build_w3 moved to chain.provider
from tx_signer import TxRevertedError, make_sign_and_send
from signing.kms_account import kms_account_from_env  # patch_kms_mm

from chain.provider import w3  # patch_trackb_shared_w3: shared handle owned outside the MM module

import logging as _logging

# patch_phase5: the MM is RETIRED. Its KMS key is being destroyed and its
# secrets removed; nothing may require them at import time. `w3` stays eager
# (it is the shared read provider six modules import); `account` and
# `sign_and_send` resolve lazily and raise a clear error only if some legacy
# direct-signing path is actually invoked (all such routes are 404-gated).
# receipt_timeout=60 + gas_estimate_fallback=250_000 reproduce the original
# behavior exactly; label/logger preserve the original warning text.
_account = None
_signer = None


def _resolve():
    global _account, _signer
    if _account is None:
        _account = kms_account_from_env("MM")  # asserts MM_KMS_KEY derives MM_ADDRESS
        _signer = make_sign_and_send(
            account=_account,
            w3=w3,
            receipt_timeout=60,
            gas_estimate_fallback=250_000,
            logger=_logging.getLogger(__name__),
            label="sign_and_send",
        )
    return _account, _signer


def sign_and_send(tx):
    """Lazy MM signer. Raises at call time (not import time) if the MM key is gone."""
    _, signer = _resolve()
    return signer(tx)


class _LazyAccount:
    """`from mm_wallet import account` stays importable; attribute access resolves the key."""

    def __getattr__(self, name):
        acct, _ = _resolve()
        return getattr(acct, name)


account = _LazyAccount()

__all__ = ["w3", "account", "sign_and_send", "TxRevertedError"]
