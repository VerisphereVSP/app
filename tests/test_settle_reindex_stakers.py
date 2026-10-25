# app/tests/test_settle_reindex_stakers.py — patch_settle_reindex_stakers
"""
A daily settlement (StakeEngine.PostUpdated) changes every lot on the post, but the indexer used to
handle the event with an empty user list, so positions (and the portfolio's Total Staked) stayed at
the value of the user's last own transaction. Now a settled post re-reads the stakers already indexed
for it. Unit level: the user-selection helper; integration level: the atomic poll loop with a
PostUpdated event refreshes chain_user_stake.amount from getUserLotInfo.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text as sql_text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_atomic_indexer import db, MockEvent  # noqa: E402,F401  (fixture + helpers)


class TestUsersToReindex:
    def _seed(self, db, pid, addrs):
        for a in addrs:
            db.execute(sql_text(
                "INSERT INTO chain_user_stake (user_address, post_id, side, amount, weighted_position, entry_epoch, tranche, position_weight, indexed_at)"
                " VALUES (:a, :p, 1, 2.0, 1.0, 0, 0, 0.5, now())"), {"a": a, "p": pid})
        db.commit()

    def test_event_users_only_when_not_settled(self, db):
        import chain_indexer
        self._seed(db, 1, ["0xaaa"])
        assert chain_indexer._users_to_reindex(db, 1, {"users": {"0xBBB"}}) == ["0xbbb"]

    def test_settled_post_adds_known_stakers(self, db):
        import chain_indexer
        self._seed(db, 1, ["0xaaa", "0xccc"])
        self._seed(db, 2, ["0xddd"])  # another post: not included
        got = chain_indexer._users_to_reindex(db, 1, {"users": {"0xBBB"}, "settled": True})
        assert got == ["0xaaa", "0xbbb", "0xccc"]

    def test_settled_post_with_nobody_indexed_is_none(self, db):
        import chain_indexer
        assert chain_indexer._users_to_reindex(db, 9, {"users": set(), "settled": True}) is None
        assert chain_indexer._users_to_reindex(db, 9, {"users": set()}) is None


class TestPollRefreshesLotsOnSettlement:
    def test_post_updated_refreshes_indexed_amount(self, db, monkeypatch):
        import chain_indexer
        # A staker indexed at 2.0 VSP on post 1, challenge side.
        db.execute(sql_text(
            "INSERT INTO chain_user_stake (user_address, post_id, side, amount, weighted_position, entry_epoch, tranche, position_weight, indexed_at)"
            " VALUES ('0x5b43dbcdd7b6092c86f62bc5f1830f266b86862b', 1, 1, 2.0, 1.0, 0, 0, 0.5, now())"))
        db.commit()
        # The chain now projects 2.000048 for that lot (support side empty).
        def lot_info(addr, pid, side):
            amt = 2_000_048_000_000_000_000 if (pid == 1 and side == 1) else 0
            return MagicMock(call=lambda: (amt, amt // 2, amt, 5 * 10**17))
        # One contract mock serves every address the canonical writer builds (StakeEngine, ScoreEngine, PostRegistry).
        c = MagicMock()
        c.functions.getUserLotInfo = lot_info
        c.functions.getPostTotals = lambda pid: MagicMock(call=lambda: (0, 2_000_048_000_000_000_000))
        c.functions.effectiveVSRay = lambda pid: MagicMock(call=lambda: -(10**18))
        c.functions.baseVSRay = lambda pid: MagicMock(call=lambda: -(10**18))
        c.functions.getPost = lambda pid: MagicMock(call=lambda: ("0xCreator", 1700000000, 1, pid, 0))  # link-type: no claim text path
        c.functions.getClaim = lambda cid: MagicMock(call=lambda: f"claim text {cid}")
        c.functions.sMax = MagicMock(return_value=MagicMock(call=lambda: 5 * 10**18))
        # Event sources for the poll loop: only one PostUpdated on post 1.
        c_se = MagicMock(); c_reg = MagicMock(); c_lg = MagicMock()
        for ev in ("StakeAdded", "StakeWithdrawn", "PostUpdated"):
            eo = MagicMock(); eo.get_logs = MagicMock(return_value=[])
            setattr(c_se.events, ev, eo)
        c_se.events.PostUpdated.get_logs = MagicMock(return_value=[
            MockEvent(50, 0, 0, {"postId": 1, "epoch": 20751, "supportTotal": 0, "challengeTotal": 2_000_048_000_000_000_000})])
        eo = MagicMock(); eo.get_logs = MagicMock(return_value=[]); c_reg.events.PostCreated = eo
        eo2 = MagicMock(); eo2.get_logs = MagicMock(return_value=[]); c_lg.events.EdgeAdded = eo2
        contracts = {
            "StakeEngine":  (c_se,  ["StakeAdded", "StakeWithdrawn", "PostUpdated"]),
            "PostRegistry": (c_reg, ["PostCreated"]),
            "LinkGraph":    (c_lg,  ["EdgeAdded"]),
        }
        w3 = MagicMock(); w3.eth.block_number = 200
        w3.eth.contract = lambda **kwargs: c
        monkeypatch.setattr(chain_indexer, "_get_w3", lambda: w3)
        monkeypatch.setattr(chain_indexer, "_get_contracts", lambda _w3: contracts)
        monkeypatch.setattr(chain_indexer, "index_global_stats_canonical", lambda _db: None)
        chain_indexer.poll_events_atomic(db)
        amt = db.execute(sql_text(
            "SELECT amount FROM chain_user_stake WHERE user_address='0x5b43dbcdd7b6092c86f62bc5f1830f266b86862b' AND post_id=1 AND side=1")).scalar()
        assert amt == pytest.approx(2.000048, abs=1e-9), amt

    def test_source_marks_settled_posts_in_both_loops(self):
        src = (ROOT / "chain_indexer.py").read_text()
        assert src.count('["settled"] = True') == 2
        assert src.count("_users_to_reindex(db, pid, meta)") == 2
        assert 'users = list(meta["users"]) or None' not in src
