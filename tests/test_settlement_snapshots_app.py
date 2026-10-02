"""patch_settlement_snapshots — app side of whitepaper v18 (settlement on stored snapshots; core #31).

  * the keeper pass runs in keeper order: claims, then links, then the claims the links point to
    (a child depends on the LINK's snapshot, so the link settles before the child);
  * one_hop(post) is what the on-demand repair settles, in that order, when the user path hits
    SettleFirst; repair_async honours a per-post cooldown and skips posts already at this epoch;
  * the relay recognises the two new ScoreEngine reverts and extracts SettleFirst's postId;
  * seed_order lists claims only, parents first (SEED_ORDER for UpgradeSnapshots).
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


def test_dependency_edges_put_link_between_parent_and_child():
    from settle_keeper import dependency_edges, topo_order
    # 1 -> 3 via link 10, 2 -> 3 via link 11, 3 -> 4 via link 12; ids deliberately out of order
    links = [(1, 3, 10), (2, 3, 11), (3, 4, 12)]
    order = topo_order([4, 3, 2, 1, 10, 11, 12], dependency_edges(links))
    pos = {p: i for i, p in enumerate(order)}
    assert pos[1] < pos[10] < pos[3] and pos[2] < pos[11] < pos[3] and pos[3] < pos[12] < pos[4]


def test_child_created_before_its_link_still_settles_after_it():
    """The soak's S1 shape: X (child) is created BEFORE P and L. Id order would settle X first."""
    from settle_keeper import dependency_edges, topo_order
    X, P, L = 1, 2, 3
    order = topo_order([X, P, L], dependency_edges([(P, X, L)]))
    assert order == [P, L, X]


def test_two_post_cycle_terminates_and_keeps_links_before_children_where_possible():
    from settle_keeper import dependency_edges, topo_order
    # A <-> B mutual challenge (links 10: A->B, 11: B->A), C hangs off B via 12
    order = topo_order([1, 2, 3, 10, 11, 12], dependency_edges([(1, 2, 10), (2, 1, 11), (2, 3, 12)]))
    assert sorted(order) == [1, 2, 3, 10, 11, 12]


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql, params=None):
        p = params["p"]
        rows = [(a, l) for (a, b, l) in self.rows if b == p]
        rows.sort(key=lambda r: r[1])
        return types.SimpleNamespace(fetchall=lambda: rows)

    def close(self):
        pass


def test_one_hop_is_parents_then_links_then_post():
    from settle_keeper import one_hop
    db = _FakeDB([(7, 3, 20), (5, 3, 21), (5, 9, 22)])
    assert one_hop(db, 3) == [5, 7, 20, 21, 3]
    assert one_hop(db, 9) == [5, 22, 9]
    assert one_hop(db, 42) == [42]


def test_repair_async_settles_hop_in_order_and_respects_cooldown(monkeypatch):
    import settle_keeper as stk
    sent = []

    class _Fn:
        def __init__(self, name, pid):
            self.name, self.pid = name, pid

        def call(self):
            return 100 if self.pid == 7 else 99  # parent 7 already at this epoch

        def build_transaction(self, _):
            return {"pid": self.pid}

    class _Functions:
        def getLastSnapshotEpoch(self, pid):
            return _Fn("last", pid)

        def updatePost(self, pid):
            return _Fn("update", pid)

    fake_sk = types.SimpleNamespace(
        is_configured=lambda: True,
        _build=lambda: None,
        _state={
            "engine": types.SimpleNamespace(functions=_Functions()),
            "sign_and_send": lambda tx: sent.append(tx["pid"]) or "0xhash",
            "account": types.SimpleNamespace(address="0xkeeper"),
            "w3": types.SimpleNamespace(eth=types.SimpleNamespace(get_block=lambda _: types.SimpleNamespace(timestamp=100 * stk.EPOCH_SEC + 5))),
        },
    )
    monkeypatch.setitem(sys.modules, "smax_keeper", fake_sk)
    stk._repair_last.clear()
    db = _FakeDB([(7, 3, 20), (5, 3, 21)])

    class _Thread:  # run synchronously
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("threading.Thread", _Thread)
    assert stk.repair_async(lambda: db, 3) is True
    assert sent == [5, 20, 21, 3]  # parent 7 skipped (already settled), order kept
    assert stk.repair_async(lambda: db, 3) is False  # cooldown
    assert sent == [5, 20, 21, 3]


def test_repair_async_noop_without_keeper(monkeypatch):
    import settle_keeper as stk
    monkeypatch.setitem(sys.modules, "smax_keeper", types.SimpleNamespace(is_configured=lambda: False))
    stk._repair_last.clear()
    assert stk.repair_async(lambda: None, 11) is False


def test_relay_knows_v18_reverts_and_extracts_settle_first_post_id():
    from relay_errors import _decode_revert_reason, _revert_selector_and_arg, SETTLE_FIRST_SELECTOR
    assert "settlement pass" in _decode_revert_reason(Exception("execution reverted: 0xbf474a94" + "00" * 64)).lower()
    assert "seeded" in _decode_revert_reason(Exception("0xb9c03aea" + "00" * 32)).lower()
    sel, pid = _revert_selector_and_arg(Exception("execution reverted: 0x1e6049a5" + "00" * 31 + "2a"))
    assert sel == SETTLE_FIRST_SELECTOR and pid == 42
    assert _revert_selector_and_arg(Exception("boom")) == (None, None)
    sel2, pid2 = _revert_selector_and_arg(Exception("revert 0xfb8e4881"))
    assert sel2 == "fb8e4881" and pid2 is None


def test_seed_order_is_claims_only_parents_first(monkeypatch):
    import settle_keeper as stk

    class _DB:
        def execute(self, sql, params=None):
            q = str(sql)
            if "FROM chain_post" in q:
                rows = [(1,), (2,), (3,), (10,), (11,)]
            elif "from_post_id, to_post_id, link_post_id" in q:
                rows = [(2, 1, 10), (3, 2, 11)]  # 3 -> 2 -> 1
            else:
                rows = [(10,), (11,)]
            return types.SimpleNamespace(fetchall=lambda: rows)

    assert stk.seed_order(_DB()) == [3, 2, 1]
