# app/tests/test_relay_gas_fee.py — patch_relay_gas_fee
"""
The relay's gas-indexed fee: one formula, one knob, no floor, no percentage;
the submission rule (never more than displayed; stale quote refused); the
collector's state machine (collect after confirmation, waive on failure,
retry then fail). Pure unit: config via monkeypatch, prices stubbed, sqlite
for the bookkeeping, no chain.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fee_calculator as fc  # noqa: E402


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    import config
    monkeypatch.setattr(config, "RELAY_FEE_ENABLED", True)
    monkeypatch.setattr(config, "RELAY_FEE_MARKUP", 0.25)
    monkeypatch.setattr(config, "RELAY_FEE_CAP_VSP", 5.0)
    monkeypatch.setattr(config, "RELAY_FEE_PRICE_MAX_AGE_SEC", 3600)
    monkeypatch.setattr(fc, "_get_avax_price_usd", lambda: 10.47)
    monkeypatch.setattr(fc, "_get_vsp_price_usd", lambda db=None: 0.2012)
    fc._last_good["avax"] = (None, 0.0); fc._last_good["vsp"] = (None, 0.0)


GAS = 260_000          # execute 200k + collect 60k
GWEI = 25 * 10**9


class TestFormula:
    def test_gas_times_markup_only(self):
        q = fc.quote_relay_fee(GAS, GWEI)
        cost_vsp = GAS * GWEI / 1e18 * 10.47 / 0.2012
        assert q["fee_total_vsp"] == pytest.approx(cost_vsp * 1.25, abs=1e-6)   # values are rounded to 6 dp
        assert q["fee_vsp"] == q["fee_total_vsp"]            # nothing on-chain -> relay collects all
        assert q["gas_cost_vsp"] == pytest.approx(cost_vsp, abs=1e-6)
        assert q["markup"] == 0.25 and q["capped"] is False and q["enabled"] is True

    def test_no_floor_small_gas_small_fee(self):
        q = fc.quote_relay_fee(1_000, GWEI)
        assert 0 < q["fee_total_vsp"] < 0.01                 # 1k gas quotes ~0.0016 VSP: no 0.1 VSP floor anywhere

    def test_independent_of_action_value(self):
        # the function has no value argument at all: a 1 VSP and a 1,000 VSP action quote alike
        import inspect
        assert "value" not in inspect.signature(fc.quote_relay_fee).parameters

    def test_knob_moves_fee_linearly(self, monkeypatch):
        import config
        base = fc.quote_relay_fee(GAS, GWEI)["fee_total_vsp"]
        monkeypatch.setattr(config, "RELAY_FEE_MARKUP", 0.0)
        assert fc.quote_relay_fee(GAS, GWEI)["fee_total_vsp"] == pytest.approx(base / 1.25, abs=2e-6)

    def test_cap_is_a_ceiling_not_a_price(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "RELAY_FEE_CAP_VSP", 0.01)
        q = fc.quote_relay_fee(GAS, GWEI)
        assert q["fee_total_vsp"] == 0.01 and q["capped"] is True

    def test_onchain_part_subtracted_during_transition(self):
        q = fc.quote_relay_fee(GAS, GWEI, onchain_fee_vsp=0.1)
        assert q["fee_vsp"] == pytest.approx(q["fee_total_vsp"] - 0.1, abs=1e-6)
        q2 = fc.quote_relay_fee(GAS, GWEI, onchain_fee_vsp=99.0)
        assert q2["fee_vsp"] == 0.0                           # never negative

    def test_disabled_means_zero(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "RELAY_FEE_ENABLED", False)
        assert fc.quote_relay_fee(GAS, GWEI)["fee_total_vsp"] == 0.0

    def test_old_knobs_are_gone(self):
        import config
        for name in ("RELAY_FEE_MARGIN_PCT", "RELAY_FEE_MIN_VSP", "RELAY_FEE_TXN_PCT"):
            assert not hasattr(config, name), name
        assert not hasattr(fc, "compute_relay_fee")


class TestOracle:
    def test_refuses_without_vsp_price(self, monkeypatch):
        def boom(db=None):
            raise fc.FeeOracleUnavailable("no pool")
        monkeypatch.setattr(fc, "_get_vsp_price_usd", boom)
        with pytest.raises(fc.FeeOracleUnavailable):
            fc.quote_relay_fee(GAS, GWEI)

    def test_last_good_vsp_price_within_window(self, monkeypatch):
        # the real reader: pool read fails, but a price was remembered 10 minutes ago
        monkeypatch.delattr(fc, "_get_vsp_price_usd")
        import importlib; importlib.reload(fc)
        import time
        fc._last_good["vsp"] = (0.25, time.time() - 600)
        monkeypatch.setattr("chain.pool_price.read_pool_state", lambda: (_ for _ in ()).throw(RuntimeError("rpc")), raising=False)
        assert fc._get_vsp_price_usd() == 0.25
        fc._last_good["vsp"] = (0.25, time.time() - 7200)   # too old
        with pytest.raises(fc.FeeOracleUnavailable):
            fc._get_vsp_price_usd()


# ── submission rule (the relay module needs a chain; test the rule in isolation) ──

def charge_rule(recomputed: float, displayed, onchain: float, stale_factor=1.5, exempt=False):
    """Mirror of the block in relay._relay_async_sync: never more than displayed;
    stale if recomputed > displayed x factor; exempt pays nothing; the on-chain
    part is subtracted from what the relay collects."""
    total = recomputed
    if displayed is not None and not exempt:
        if total > float(displayed) * stale_factor:
            raise ValueError("stale")
        total = min(total, float(displayed))
    if exempt:
        total = 0.0
    return total, max(0.0, round(total - onchain, 6))


class TestSubmissionRule:
    def test_never_more_than_displayed(self):
        assert charge_rule(0.40, 0.35, 0.0) == (0.35, 0.35)

    def test_recomputed_lower_wins(self):
        assert charge_rule(0.30, 0.35, 0.0) == (0.30, 0.30)

    def test_stale_quote_refused(self):
        with pytest.raises(ValueError):
            charge_rule(0.60, 0.35, 0.0)

    def test_no_display_means_recomputed(self):
        assert charge_rule(0.33, None, 0.0) == (0.33, 0.33)

    def test_transition_onchain_subtracted(self):
        total, relay_part = charge_rule(0.35, 0.35, 0.10)
        assert total == 0.35 and relay_part == pytest.approx(0.25)

    def test_exempt_pays_nothing(self):
        assert charge_rule(0.35, 0.35, 0.0, exempt=True) == (0.0, 0.0)

    def test_relay_source_contains_the_rule(self):
        src = (ROOT / "relay.py").read_text()
        assert "fee_total_vsp = min(fee_total_vsp, float(displayed))" in src
        assert "RELAY_FEE_QUOTE_STALE_FACTOR" in src
        assert "fee_quote_vsp=fee_total_vsp" in src
        assert 'collector' in src and '"basis": "gas"' in src


# ── collector state machine (sqlite) ──

SCHEMA = """
CREATE TABLE tx_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tx_hash TEXT NOT NULL UNIQUE, user_address TEXT NOT NULL,
  to_address TEXT NOT NULL, calldata TEXT NOT NULL, action_type TEXT NOT NULL, action_value REAL,
  status TEXT NOT NULL DEFAULT 'pending', block_number INTEGER, gas_used INTEGER, post_id INTEGER,
  error_message TEXT, submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT,
  fee_quote_vsp REAL, fee_charge_vsp REAL, fee_status TEXT NOT NULL DEFAULT 'none',
  fee_tx_hash TEXT, fee_attempts INTEGER NOT NULL DEFAULT 0, fee_error TEXT
);
"""
FEE_LOG = "CREATE TABLE relay_fee_log (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_hash TEXT, user_address TEXT, fee_charged_vsp REAL, tx_type TEXT, gas_cost_avax REAL);"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(sql_text(SCHEMA)); conn.execute(sql_text(FEE_LOG))
    s = sessionmaker(bind=engine, future=True)()
    yield s
    s.close()


def _row(db, h, status, charge, attempts=0):
    import tx_log
    rid = tx_log.record_pending(db, h, "0xUser", "0xTarget", "0x00", action_type="stake", action_value=1.0,
                                fee_quote_vsp=charge, fee_charge_vsp=charge)
    db.execute(sql_text("UPDATE tx_log SET status=:s, fee_attempts=:a WHERE id=:id"), {"s": status, "a": attempts, "id": rid})
    db.execute(sql_text("INSERT INTO relay_fee_log (tx_hash, user_address, tx_type) VALUES (:h, 'u', 'stake')"), {"h": h})
    db.commit()
    return rid


def _status(db, rid):
    return db.execute(sql_text("SELECT fee_status, fee_tx_hash, fee_attempts, fee_error FROM tx_log WHERE id=:id"), {"id": rid}).fetchone()


class TestCollector:
    def test_record_pending_sets_fee_status(self, db):
        import tx_log
        a = tx_log.record_pending(db, "0x01", "0xU", "0xT", "0x", "stake", 1.0, fee_quote_vsp=0.3, fee_charge_vsp=0.3)
        b = tx_log.record_pending(db, "0x02", "0xU", "0xT", "0x", "stake", 1.0, fee_quote_vsp=0.3, fee_charge_vsp=0.0)
        c = tx_log.record_pending(db, "0x03", "0xU", "0xT", "0x", "stake", 1.0)
        assert _status(db, a)[0] == "pending" and _status(db, b)[0] == "none" and _status(db, c)[0] == "none"

    def test_collects_confirmed_only_and_fills_fee_log(self, db):
        from fee_collector import collect_due_fees
        confirmed = _row(db, "0xaa", "confirmed", 0.3)
        pending = _row(db, "0xbb", "pending", 0.3)
        sent = []
        out = collect_due_fees(db, send=lambda user, wei: sent.append((user, wei)) or "0xfee1")
        assert out["collected"] == 1 and sent == [("0xuser", int(0.3e18))]
        assert _status(db, confirmed)[:2] == ("collected", "0xfee1")
        assert _status(db, pending)[0] == "pending"
        charged = db.execute(sql_text("SELECT fee_charged_vsp FROM relay_fee_log WHERE tx_hash='0xaa'")).scalar()
        assert charged == pytest.approx(0.3)
        # second pass: nothing to do, nothing double-charged
        out2 = collect_due_fees(db, send=lambda u, w: "0xdup")
        assert out2["collected"] == 0 and _status(db, confirmed)[1] == "0xfee1"

    def test_reverted_and_dropped_are_waived(self, db):
        from fee_collector import collect_due_fees
        r = _row(db, "0xcc", "reverted", 0.3); d = _row(db, "0xdd", "dropped", 0.3)
        out = collect_due_fees(db, send=lambda u, w: pytest.fail("must not send"))
        assert out["waived"] == 2 and _status(db, r)[0] == "waived" and _status(db, d)[0] == "waived"

    def test_retry_then_fail(self, db, monkeypatch):
        import fee_collector
        monkeypatch.setattr(fee_collector, "MAX_ATTEMPTS", 2)
        rid = _row(db, "0xee", "confirmed", 0.3)
        def boom(u, w): raise RuntimeError("nonce too low")
        out1 = collect_due_fees = fee_collector.collect_due_fees(db, send=boom)
        assert out1["retried"] == 1 and _status(db, rid)[0] == "pending" and _status(db, rid)[2] == 1
        out2 = fee_collector.collect_due_fees(db, send=boom)
        assert out2["failed"] == 1 and _status(db, rid)[0] == "failed" and "nonce too low" in _status(db, rid)[3]
        out3 = fee_collector.collect_due_fees(db, send=boom)   # final: not retried again
        assert out3["failed"] == 0 and out3["retried"] == 0

    def test_disabled_collects_nothing(self, db, monkeypatch):
        import config
        from fee_collector import collect_due_fees
        monkeypatch.setattr(config, "RELAY_FEE_ENABLED", False)
        rid = _row(db, "0xff", "confirmed", 0.3)
        out = collect_due_fees(db, send=lambda u, w: pytest.fail("disabled"))
        assert out["collected"] == 0 and _status(db, rid)[0] == "pending"
