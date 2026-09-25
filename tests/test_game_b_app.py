"""patch_game_b — app side of evidence-economic settlement.

  * settle_keeper.topo_order settles parents before children (Kahn), links after their parent,
    and terminates on cycles.
  * relay maps SettleFirst / InexactScore / WhenPaused to honest messages (kill-switch drill
    2026-09-24 showed a pause read as a gas error).
  * chain.vs is still the only VS path (base_vs mirrors the one score).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


def test_topo_order_parents_first():
    from settle_keeper import topo_order
    # 1 -> 3, 2 -> 3, 3 -> 4 ; links 10 (1->3), 11 (2->3), 12 (3->4) hang off their parents
    posts = [4, 3, 2, 1, 10, 11, 12]
    edges = [(1, 3), (2, 3), (3, 4), (1, 10), (2, 11), (3, 12)]
    order = topo_order(posts, edges)
    pos = {p: i for i, p in enumerate(order)}
    assert pos[1] < pos[3] and pos[2] < pos[3] and pos[3] < pos[4]
    assert pos[1] < pos[10] and pos[2] < pos[11] and pos[3] < pos[12]
    assert sorted(order) == sorted(posts)


def test_topo_order_cycle_terminates():
    from settle_keeper import topo_order
    order = topo_order([1, 2, 3], [(1, 2), (2, 1), (2, 3)])
    assert sorted(order) == [1, 2, 3]


def test_relay_maps_settlement_and_pause_reverts():
    from relay_errors import _decode_revert_reason
    assert "settled" in _decode_revert_reason(Exception("execution reverted: 0x1e6049a5" + "00" * 32)).lower()
    assert "scored exactly" in _decode_revert_reason(Exception("0x609b0047" + "00" * 32)).lower()
    assert "paused" in _decode_revert_reason(Exception("revert 0xfb8e4881")).lower()


def test_no_off_chain_vs_or_edge_formula_survives():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    for dirpath, _, files in os.walk(root):
        if "tests" in dirpath or "node_modules" in dirpath or "contracts" in dirpath:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            src = open(p, encoding="utf-8").read()
            if p.endswith("chain/vs.py"):
                continue
            if "link_eff" in src or "sum_outgoing" in src or "read_base_vs_pct(" in src:
                bad.append(p)
    assert not bad, f"off-chain VS/edge math survives in: {bad}"
