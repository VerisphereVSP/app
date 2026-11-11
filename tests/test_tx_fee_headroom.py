"""patch_tx_fee_headroom: the signer prices with headroom (2x base fee + a floored priority), a receipt
timeout is a failure for the keepers (TxTimeoutError) and still a warned return for the relay family, the
settle pass retries an epoch whose sends did not land, and the sMax keeper stops poking on an unconfirmed poke.
No chain: web3 is faked at the eth-API surface; web3.exceptions is stubbed when the package is absent."""
import sys
import types

import pytest

try:
    from web3.exceptions import TimeExhausted
except Exception:  # pragma: no cover - local runs without web3
    TimeExhausted = type("TimeExhausted", (Exception,), {})
    sys.modules.setdefault("web3", types.ModuleType("web3"))
    sys.modules["web3.exceptions"] = types.SimpleNamespace(TimeExhausted=TimeExhausted)

GWEI = 1_000_000_000


class _Signed:
    raw_transaction = b"\x01\x02"


class _Account:
    address = "0x000000000000000000000000000000000000beef"

    def sign_transaction(self, tx):
        self.last = dict(tx)
        return _Signed()


def _w3(*, base_fee=int(7.5 * GWEI), priority=150, receipt="ok", block_raises=False):
    """A web3 stand-in: the eth calls make_sign_and_send touches, nothing else."""
    def get_block(_tag):
        if block_raises:
            raise RuntimeError("no base fee")
        return types.SimpleNamespace(baseFeePerGas=base_fee)

    def wait(_h, timeout):
        if receipt == "timeout":
            raise TimeExhausted("x")
        return types.SimpleNamespace(status=0 if receipt == "revert" else 1, blockNumber=1)

    eth = types.SimpleNamespace(
        get_block=get_block, max_priority_fee=priority, gas_price=int(7.5 * GWEI),
        get_transaction_count=lambda _a, _t: 1642, chain_id=43114, estimate_gas=lambda _tx: 90_000,
        send_raw_transaction=lambda _raw: bytes.fromhex("ab" * 32), wait_for_transaction_receipt=wait)
    return types.SimpleNamespace(eth=eth)


def _signer(w3, **kw):
    from tx_signer import make_sign_and_send
    acct = _Account()
    return acct, make_sign_and_send(account=acct, w3=w3, receipt_timeout=120, gas_estimate_fallback=None, **kw)


def test_fee_ceiling_is_twice_base_plus_floored_priority():
    import tx_signer
    acct, send = _signer(_w3(priority=150))  # Avalanche suggests 150 wei
    assert send({"from": acct.address, "to": acct.address, "data": "0x"}) == "0x" + "ab" * 32
    tx = acct.last
    assert tx["type"] == 2
    assert tx["maxPriorityFeePerGas"] == tx_signer.TX_PRIORITY_FLOOR_WEI == 1 * GWEI
    assert tx["maxFeePerGas"] == 2 * int(7.5 * GWEI) + 1 * GWEI
    assert tx["nonce"] == 1642 and tx["chainId"] == 43114 and tx["gas"] == 90_000
    # a real priority suggestion above the floor keeps its 1.5x
    acct, send = _signer(_w3(priority=2 * GWEI))
    send({"from": acct.address})
    assert acct.last["maxPriorityFeePerGas"] == 3 * GWEI
    assert acct.last["maxFeePerGas"] == 2 * int(7.5 * GWEI) + 3 * GWEI


def test_legacy_fallback_also_carries_headroom():
    acct, send = _signer(_w3(block_raises=True))
    send({"from": acct.address, "gasPrice": 1})  # a caller's gasPrice is replaced, never trusted
    gp = int(7.5 * GWEI)
    assert "maxFeePerGas" not in acct.last
    assert acct.last["gasPrice"] == max(gp * 120 // 100, gp + 1 * GWEI) == 9 * GWEI
    w3 = _w3(block_raises=True); w3.eth.gas_price = 2 * GWEI  # a low gas price: the floor, not the 20 %, carries the headroom
    acct, send = _signer(w3)
    send({"from": acct.address})
    assert acct.last["gasPrice"] == 3 * GWEI


def test_timeout_returns_for_the_relay_family_and_raises_for_keepers(caplog):
    from tx_signer import TxTimeoutError, TxRevertedError
    acct, send = _signer(_w3(receipt="timeout"))
    assert send({"from": acct.address}) == "0x" + "ab" * 32  # default: warned return (relay watcher resolves it)
    acct, send = _signer(_w3(receipt="timeout"), on_timeout="raise")
    with pytest.raises(TxTimeoutError) as ei:
        send({"from": acct.address})
    assert ei.value.tx_hash == "0x" + "ab" * 32 and ei.value.timeout == 120
    acct, send = _signer(_w3(receipt="revert"), on_timeout="raise")
    with pytest.raises(TxRevertedError):
        send({"from": acct.address})
    with pytest.raises(ValueError):
        _signer(_w3(), on_timeout="ignore")


# ── settle keeper: an epoch whose sends did not land is retried, a late landing is skipped ──────────────

class _Fn:
    def __init__(self, kind, pid, epochs):
        self.kind, self.pid, self.epochs = kind, pid, epochs

    def call(self):
        return self.epochs.get(self.pid, 0)

    def build_transaction(self, _):
        return {"pid": self.pid}


class _Functions:
    def __init__(self, epochs):
        self.epochs = epochs

    def getLastSnapshotEpoch(self, pid):
        return _Fn("last", pid, self.epochs)

    def updatePost(self, pid):
        return _Fn("update", pid, self.epochs)


class _GraphDB:
    def execute(self, sql, params=None):
        s = str(sql)
        rows = [(1,), (2,), (3,)] if "chain_post" in s else []
        return types.SimpleNamespace(fetchall=lambda: rows)

    def close(self):
        pass

    def commit(self):
        pass


def _wire_settle(monkeypatch, epochs, send):
    import settle_keeper as stk
    fake_sk = types.SimpleNamespace(
        is_configured=lambda: True, _build=lambda: None,
        _state={"engine": types.SimpleNamespace(functions=_Functions(epochs)), "sign_and_send": send,
                "account": types.SimpleNamespace(address="0xkeeper"),
                "w3": types.SimpleNamespace(eth=types.SimpleNamespace(
                    get_block=lambda _: types.SimpleNamespace(timestamp=20768 * stk.EPOCH_SEC + stk.MARGIN_SEC + 5)))})
    monkeypatch.setitem(sys.modules, "smax_keeper", fake_sk)
    alerts = []
    monkeypatch.setitem(sys.modules, "notify", types.SimpleNamespace(send_alert=lambda *a, **k: alerts.append((a, k))))
    metrics = []
    import balance_sampler
    monkeypatch.setattr(balance_sampler, "record_metric", lambda db, m, v, labels=None: metrics.append((m, v, labels)))
    stk._last_pass_epoch = -1
    return stk, alerts, metrics


def test_settle_pass_with_an_unconfirmed_send_is_retried_next_cycle(monkeypatch):
    from tx_signer import TxTimeoutError
    epochs = {1: 20767, 2: 20767, 3: 20768}  # 3 settled already this epoch
    sent = []

    def send(tx):
        sent.append(tx["pid"])
        if tx["pid"] == 2:
            raise TxTimeoutError("0xdead", 120)
        epochs[tx["pid"]] = 20768
        return "0xok"

    stk, alerts, metrics = _wire_settle(monkeypatch, epochs, send)
    stk.poll_once(lambda: _GraphDB())
    assert sent == [1, 2]                      # 3 skipped: already at the epoch
    assert stk._last_pass_epoch == -1          # not done: post 2 did not land
    assert [m for m in metrics if m[0] == "settle_pass_failed"][0][1] == 1
    assert [m for m in metrics if m[0] == "settle_pass_settled"][0][1] == 1
    assert alerts and alerts[0][1]["post_id"] == 2 and "no receipt" in alerts[0][1]["detail"]
    # next cycle: post 2 landed late on chain → skipped, not resent; the pass completes
    epochs[2] = 20768
    stk.poll_once(lambda: _GraphDB())
    assert sent == [1, 2]
    assert stk._last_pass_epoch == 20768


def test_settle_pass_marks_the_epoch_done_only_when_every_send_landed(monkeypatch):
    epochs = {1: 20767, 2: 20767, 3: 20767}
    sent = []

    def send(tx):
        sent.append(tx["pid"]); epochs[tx["pid"]] = 20768; return "0x" + "11" * 32

    stk, alerts, metrics = _wire_settle(monkeypatch, epochs, send)
    stk.poll_once(lambda: _GraphDB())
    assert sent == [1, 2, 3] and stk._last_pass_epoch == 20768 and not alerts
    stk.poll_once(lambda: _GraphDB())  # same epoch: nothing more is sent
    assert sent == [1, 2, 3]


# ── sMax keeper: an unconfirmed poke is False (the cycle stops), not "landed" ──────────────────────────

def test_smax_poke_unconfirmed_is_a_failure_and_alerts(monkeypatch):
    import smax_keeper as sk
    from tx_signer import TxTimeoutError
    alerts = []
    monkeypatch.setitem(sys.modules, "notify", types.SimpleNamespace(send_alert=lambda *a, **k: alerts.append(k)))
    calls = []

    class _Refresh:
        def __init__(self, pid): self.pid = pid
        def build_transaction(self, _): return {"pid": self.pid}

    def send(tx):
        calls.append(tx["pid"]); raise TxTimeoutError("0xbeef", 120)

    monkeypatch.setitem(sk._state, "engine", types.SimpleNamespace(functions=types.SimpleNamespace(refreshSMax=_Refresh)))
    monkeypatch.setitem(sk._state, "account", types.SimpleNamespace(address="0xkeeper"))
    monkeypatch.setitem(sk._state, "sign_and_send", send)
    assert sk._poke(26, "post 26 total > sMax") is False
    assert calls == [26]
    assert alerts and alerts[0]["post_id"] == 26 and "unconfirmed" in alerts[0]["detail"]


def test_keeper_signer_is_built_with_on_timeout_raise():
    import inspect
    import smax_keeper as sk
    src = inspect.getsource(sk._build)
    assert 'on_timeout="raise"' in src
