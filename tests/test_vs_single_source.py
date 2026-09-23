"""patch_vs_single_source — the app has exactly one VS read path and one
group aggregator. These tests fail on the pre-patch tree:

  * chain_reader.get_verity_score silently fell back to a stake-share formula
  * dupe_groups._refresh_group_stats computed base_vs + link_eff (VSP added to %)
  * unified_grouping fell back to a simple mean for unstaked groups
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from chain.vs import ray_to_pct, stake_weighted_vs, read_effective_vs_pct  # noqa: E402


# ── conversion ──────────────────────────────────────────────────────────────

def test_ray_to_pct():
    assert ray_to_pct(10 ** 18) == 100.0
    assert ray_to_pct(5 * 10 ** 17) == 50.0
    assert ray_to_pct(-10 ** 18) == -100.0
    assert ray_to_pct(0) == 0.0


# ── group aggregation: stake-weighted mean of member chain VS, nothing else ──

def test_stake_weighted_vs_is_weighted_mean():
    # (vs, stake): 100% on 2.0 and -100% on 1.0 -> (200 - 100) / 3 = +33.33
    assert stake_weighted_vs([(100.0, 2.0), (-100.0, 1.0)]) == pytest.approx(100.0 / 3.0)


def test_stake_weighted_vs_unstaked_members_have_no_weight():
    assert stake_weighted_vs([(100.0, 0.0), (-40.0, 5.0)]) == pytest.approx(-40.0)


def test_stake_weighted_vs_unstaked_group_is_zero():
    assert stake_weighted_vs([(100.0, 0.0), (80.0, 0.0)]) == 0.0
    assert stake_weighted_vs([]) == 0.0


def test_stake_weighted_vs_clamps():
    assert stake_weighted_vs([(150.0, 1.0)]) == 100.0
    assert stake_weighted_vs([(-150.0, 1.0)]) == -100.0


# ── chain read: raises, never fabricates ────────────────────────────────────

class _Boom:
    class functions:  # noqa: N801
        @staticmethod
        def effectiveVSRay(_pid):
            class _C:
                def call(self):
                    raise ConnectionError("rpc down")
            return _C()


class _Ok:
    class functions:  # noqa: N801
        @staticmethod
        def effectiveVSRay(_pid):
            class _C:
                def call(self):
                    return 5 * 10 ** 17
            return _C()


def test_read_effective_vs_pct_no_fallback():
    with pytest.raises(ConnectionError):
        read_effective_vs_pct(_Boom(), 1)
    assert read_effective_vs_pct(_Ok(), 1) == 50.0


def test_chain_reader_get_verity_score_raises_instead_of_stake_share(monkeypatch):
    pytest.importorskip("web3")
    import chain.chain_reader as cr
    cr.clear_cache()
    monkeypatch.setattr(cr, "_get_score_engine", lambda: _Boom())
    # pre-patch: returned (support/total)*100 from a second RPC read; now: raises
    monkeypatch.setattr(cr, "get_stake_totals", lambda pid: (3.0, 1.0))
    with pytest.raises(ConnectionError):
        cr.get_verity_score(1)


# ── dupe groups: weighted mean of members, links ignored ────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Just enough Session for _refresh_group_stats."""

    def __init__(self, members):
        self.members = members
        self.updates = []
        self.link_queries = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "WHERE c.dupe_group_id" in sql:
            return _Result(self.members)
        if "UPDATE claim_dupe_group" in sql:
            self.updates.append(params)
            return _Result([])
        if "chain_link" in sql:
            self.link_queries += 1
            return _Result([])
        if "embedding <=>" in sql:
            return _Result([])  # no embeddings -> no ejection path
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def commit(self):
        pass


def _stub_sqlalchemy_if_missing():
    """dupe_groups imports sqlalchemy at module level; on the dev host it lives
    only in the app container. This test exercises arithmetic, not SQL, and
    its FakeDB treats statements as strings, so a minimal stub is faithful."""
    try:
        import sqlalchemy  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    import types
    sa = types.ModuleType("sqlalchemy")
    sa.text = lambda s: s
    orm = types.ModuleType("sqlalchemy.orm")
    orm.Session = object
    sa.orm = orm
    sys.modules["sqlalchemy"] = sa
    sys.modules["sqlalchemy.orm"] = orm


def test_dupe_group_aggregate_is_stake_weighted_mean_and_ignores_links():
    _stub_sqlalchemy_if_missing()
    import dupe_groups

    # (post_id, text, support, challenge, effective_vs) — mainnet-claim-#1-like
    # member at +50% with 2.0 stake, plus a -100% member with 1.0 stake.
    members = [
        (1, "a", 2.0, 0.0, 50.0),
        (2, "b", 0.0, 1.0, -100.0),
    ]
    db = _FakeDB(members)
    dupe_groups._refresh_group_stats(db, 7)

    assert db.link_queries == 0, "links must not be consulted for a group VS"
    assert len(db.updates) == 1
    upd = db.updates[0]
    # (50*2 + -100*1) / 3 = 0.0
    assert upd["avs"] == pytest.approx(0.0)
    assert upd["ts"] == 2.0 and upd["tc"] == 1.0 and upd["mc"] == 2
    assert upd["gid"] == 7


# ── unified grouping: same aggregator, unstaked group reads 0 ───────────────

def test_unified_grouping_unstaked_group_is_zero_and_hidden():
    from unified_grouping import Item, KIND_CLAIM, _lane_and_visibility

    a = Item(kind=KIND_CLAIM, id=1, text="x", stake=0.0, vs=80.0)
    b = Item(kind=KIND_CLAIM, id=2, text="y", stake=0.0, vs=60.0)
    lane, visible, agg_stake, agg_vs = _lane_and_visibility([a, b], claim_anchored=True)
    assert agg_vs == 0.0
    assert visible is False


def test_unified_grouping_stake_weighted():
    from unified_grouping import Item, KIND_CLAIM, _lane_and_visibility

    a = Item(kind=KIND_CLAIM, id=1, text="x", stake=3.0, vs=100.0)
    b = Item(kind=KIND_CLAIM, id=2, text="y", stake=1.0, vs=-100.0)
    lane, visible, agg_stake, agg_vs = _lane_and_visibility([a, b], claim_anchored=True)
    assert agg_vs == pytest.approx(50.0)
    assert visible is True
