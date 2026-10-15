# tests/test_smax_keeper_check.py — patch_smax_keeper_check
"""The keeper writes one ops_metrics row per completed cycle (metric smax_keeper_check, value = pokes), on every
exit of poll_once: honest / no posts / poke failed / cap reached. No chain, no db: the recorder's writes are captured
by monkeypatching balance_sampler.record_metric and db.get_session_factory; poll_once runs against a fake engine."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import smax_keeper as sk  # noqa: E402

E = 10**18


class _FakeDB:
    def __init__(self):
        self.committed = False
        self.closed = False

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.fixture
def captured(monkeypatch):
    rows = []
    dbs = []

    def fake_record_metric(db, metric, value, labels=None):
        rows.append((metric, value, labels))

    def fake_factory():
        def make():
            d = _FakeDB()
            dbs.append(d)
            return d
        return make

    import balance_sampler
    import db as dbmod
    monkeypatch.setattr(balance_sampler, "record_metric", fake_record_metric)
    monkeypatch.setattr(dbmod, "get_session_factory", fake_factory)
    return rows, dbs


def test_record_check_writes_one_row_and_commits(captured):
    rows, dbs = captured
    sk._record_check(1234, 300 * E, 0, "sMax honest; nothing to do")
    assert rows == [("smax_keeper_check", 0, {"epoch": 1234, "smax_vsp": 300.0, "reason": "sMax honest; nothing to do"})]
    assert dbs and dbs[0].committed and dbs[0].closed


def test_record_check_never_raises(monkeypatch):
    import db as dbmod

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(dbmod, "get_session_factory", boom)
    sk._record_check(1, None, 0, "x")  # must not raise


def test_record_check_without_smax_and_long_reason(captured):
    rows, _ = captured
    sk._record_check(7, None, 2, "r" * 500)
    m, v, labels = rows[0]
    assert v == 2 and labels["smax_vsp"] is None and len(labels["reason"]) == 120


def _fake_engine(smax, smax_last, totals):
    """engine.functions.X(...).call() with the three views poll_once reads."""
    class _Call:
        def __init__(self, v):
            self._v = v

        def call(self):
            return self._v

    class _Fns:
        def sMax(self):
            return _Call(smax)

        def sMaxLastUpdatedEpoch(self):
            return _Call(smax_last)

        def getPostTotals(self, pid):
            return _Call((totals[pid], 0))  # (support, challenge)

    return types.SimpleNamespace(functions=_Fns())


@pytest.fixture
def wired(monkeypatch):
    """poll_once with the chain, the signer and the db shortlist faked; the recorder captured."""
    calls = []
    monkeypatch.setenv("KEEPER_KMS_KEY", "fake")
    monkeypatch.setenv("KEEPER_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setattr(sk, "_build", lambda: None)
    monkeypatch.setattr(sk, "_check_gas_balance", lambda: None)
    monkeypatch.setattr(sk, "_get_cursor_epoch", lambda: -1)
    monkeypatch.setattr(sk, "_set_cursor_epoch", lambda e: None)
    monkeypatch.setattr(sk, "_record_check", lambda epoch, smax, pokes, reason: calls.append((epoch, smax, pokes, reason)))
    w3 = types.SimpleNamespace(eth=types.SimpleNamespace(get_block=lambda _: types.SimpleNamespace(timestamp=100 * sk.KEEPER_EPOCH_SEC)))
    sk._state.update({"built": True, "w3": w3})
    return calls


def test_poll_once_records_honest_cycle(wired, monkeypatch):
    sk._state["engine"] = _fake_engine(300 * E, 100, {1: 300 * E})
    monkeypatch.setattr(sk, "_db_candidates", lambda n: [1])
    sk.poll_once()
    assert wired == [(100, 300 * E, 0, "sMax honest; nothing to do")]


def test_poll_once_records_no_posts(wired, monkeypatch):
    sk._state["engine"] = _fake_engine(0, 0, {})
    monkeypatch.setattr(sk, "_db_candidates", lambda n: [])
    sk.poll_once()
    assert wired == [(100, None, 0, "no posts indexed yet")]


def test_poll_once_records_failed_poke(wired, monkeypatch):
    sk._state["engine"] = _fake_engine(300 * E, 100, {7: 400 * E})
    monkeypatch.setattr(sk, "_db_candidates", lambda n: [7])
    monkeypatch.setattr(sk, "_poke", lambda pid, reason: False)
    sk.poll_once()
    assert len(wired) == 1 and wired[0][2] == 1 and wired[0][3] == "poke failed: post 7"
