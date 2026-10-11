# app/tests/test_tx_log.py
"""
Unit tests for Bundle 4a — tx_log + confirmation watcher.

Covers:
  - tx_log.record_pending writes the expected row
  - tx_log.mark_confirmed/reverted/dropped only update pending rows (idempotent)
  - Watcher state machine: receipt status=1 → confirmed, status=0 → reverted,
    receipt not found + age beyond timeout → dropped
  - Notifications endpoint returns pending+recent in expected shape

Uses sqlite in-memory for DB and stubs web3 with simple mock objects.
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

# Add app/ to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── Schema fixture: minimal tx_log for tests ──────────────────────
TX_LOG_SCHEMA_SQLITE = """
CREATE TABLE tx_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_hash         TEXT NOT NULL UNIQUE,
  user_address    TEXT NOT NULL,
  to_address      TEXT NOT NULL,
  calldata        TEXT NOT NULL,
  action_type     TEXT NOT NULL,
  action_value    REAL,
  status          TEXT NOT NULL DEFAULT 'pending',
  block_number    INTEGER,
  gas_used        INTEGER,
  post_id         INTEGER,
  error_message   TEXT,
  submitted_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at     TEXT,
  fee_quote_vsp   REAL,
  fee_charge_vsp  REAL,
  fee_status      TEXT NOT NULL DEFAULT 'none',
  fee_tx_hash     TEXT,
  fee_attempts    INTEGER NOT NULL DEFAULT 0,
  fee_error       TEXT
);
"""
# patch_relay_gas_fee: the six fee columns (migration 320) ride tx_log


@pytest.fixture
def db():
    """A fresh in-memory sqlite session per test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(sql_text(TX_LOG_SCHEMA_SQLITE))
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    yield s
    s.close()


# ── tx_log helpers ────────────────────────────────────────────────

class TestRecordPending:
    def test_inserts_pending_row(self, db):
        import tx_log
        rid = tx_log.record_pending(
            db, "0xabc123", "0xUser", "0xTarget", "0xdeadbeef",
            action_type="stake", action_value=1.5,
        )
        db.commit()
        assert rid is not None and rid > 0

        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row is not None
        assert row.tx_hash == "0xabc123"
        assert row.user_address == "0xuser"  # lowercased
        assert row.to_address == "0xtarget"
        assert row.calldata == "0xdeadbeef"
        assert row.action_type == "stake"
        assert row.action_value == 1.5
        assert row.status == "pending"

    def test_normalizes_tx_hash_lowercase(self, db):
        import tx_log
        tx_log.record_pending(db, "0xABCDEF", "0xUser", "0xTo", "0x00",
                              action_type="claim")
        db.commit()
        row = db.execute(sql_text("SELECT tx_hash FROM tx_log")).first()
        assert row.tx_hash == "0xabcdef"

    def test_idempotent_same_tx_hash(self, db):
        import tx_log
        rid1 = tx_log.record_pending(db, "0xaa", "0xu", "0xt", "0x00", "stake")
        db.commit()
        rid2 = tx_log.record_pending(db, "0xaa", "0xu", "0xt", "0x00", "stake")
        db.commit()
        assert rid1 == rid2
        count = db.execute(sql_text("SELECT COUNT(*) FROM tx_log")).scalar()
        assert count == 1


class TestStatusTransitions:
    def test_mark_confirmed_only_when_pending(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xa", "0xu", "0xt", "0x00", "stake")
        db.commit()
        ok = tx_log.mark_confirmed(db, rid, block_number=42, gas_used=100, post_id=7)
        db.commit()
        assert ok is True
        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "confirmed"
        assert row.block_number == 42
        assert row.gas_used == 100
        assert row.post_id == 7
        # Second call is a no-op
        ok2 = tx_log.mark_confirmed(db, rid, block_number=999, gas_used=999)
        db.commit()
        assert ok2 is False
        row2 = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row2.block_number == 42  # unchanged

    def test_mark_reverted(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xb", "0xu", "0xt", "0x00", "stake")
        db.commit()
        ok = tx_log.mark_reverted(db, rid, "Out of gas", block_number=10, gas_used=50)
        db.commit()
        assert ok is True
        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "reverted"
        assert row.error_message == "Out of gas"

    def test_mark_dropped(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xc", "0xu", "0xt", "0x00", "claim")
        db.commit()
        ok = tx_log.mark_dropped(db, rid)
        db.commit()
        assert ok is True
        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "dropped"
        assert "not found" in (row.error_message or "").lower()

    def test_cannot_overwrite_resolved_status(self, db):
        """Once a row is confirmed/reverted/dropped, further marks are no-ops."""
        import tx_log
        rid = tx_log.record_pending(db, "0xd", "0xu", "0xt", "0x00", "stake")
        db.commit()
        tx_log.mark_confirmed(db, rid, block_number=1, gas_used=1)
        db.commit()
        ok_revert = tx_log.mark_reverted(db, rid, "trying to overwrite")
        ok_drop = tx_log.mark_dropped(db, rid)
        db.commit()
        assert ok_revert is False
        assert ok_drop is False
        row = db.execute(sql_text("SELECT status FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "confirmed"


# ── Watcher state machine (simulated against the helpers) ──────────

class FakeReceipt:
    def __init__(self, status, block_number, gas_used):
        self.status = status
        self.blockNumber = block_number
        self.gasUsed = gas_used


class FakeWeb3Eth:
    """Minimal w3.eth stub supporting get_transaction_receipt."""
    def __init__(self, receipts):
        # receipts: dict of tx_hash -> FakeReceipt | Exception
        self._receipts = receipts

    def get_transaction_receipt(self, tx_hash):
        r = self._receipts.get(tx_hash)
        if isinstance(r, Exception):
            raise r
        if r is None:
            # Mimic web3 raising TransactionNotFound
            raise Exception("TransactionNotFound: " + tx_hash)
        return r


class TestWatcherSemantics:
    """Test the *semantic* state-machine logic of the watcher. The actual
    resolve_pending_txs in chain_indexer.py is tested integrally — here we
    exercise the decision tree using the helpers directly."""

    def _simulate_resolve(self, db, eth, pending_rows, timeout_seconds=600):
        """Replicates resolve_pending_txs logic against in-memory state."""
        import tx_log
        for row in pending_rows:
            try:
                receipt = eth.get_transaction_receipt(row.tx_hash)
            except Exception:
                if row.age_sec > timeout_seconds:
                    tx_log.mark_dropped(db, row.id)
                continue
            if receipt.status == 1:
                tx_log.mark_confirmed(db, row.id, receipt.blockNumber, receipt.gasUsed)
            else:
                tx_log.mark_reverted(db, row.id, "Transaction reverted",
                                     block_number=receipt.blockNumber,
                                     gas_used=receipt.gasUsed)

    def test_status_1_becomes_confirmed(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xab", "0xu", "0xt", "0x00", "stake")
        db.commit()
        # Bypass postgres-specific get_pending_for_watcher: fake a row
        FakeRow = types.SimpleNamespace
        rows = [FakeRow(id=rid, tx_hash="0xab", age_sec=10)]
        eth = FakeWeb3Eth({"0xab": FakeReceipt(status=1, block_number=100, gas_used=200)})
        self._simulate_resolve(db, eth, rows)
        db.commit()
        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "confirmed"
        assert row.block_number == 100

    def test_status_0_becomes_reverted(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xcd", "0xu", "0xt", "0x00", "stake")
        db.commit()
        FakeRow = types.SimpleNamespace
        rows = [FakeRow(id=rid, tx_hash="0xcd", age_sec=10)]
        eth = FakeWeb3Eth({"0xcd": FakeReceipt(status=0, block_number=100, gas_used=50)})
        self._simulate_resolve(db, eth, rows)
        db.commit()
        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "reverted"

    def test_not_found_under_timeout_stays_pending(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xee", "0xu", "0xt", "0x00", "stake")
        db.commit()
        # Mock get_pending_for_watcher to return age_sec=10 (under timeout)
        FakeRow = types.SimpleNamespace
        rows = [FakeRow(id=rid, tx_hash="0xee", age_sec=10)]
        eth = FakeWeb3Eth({})  # no receipt for this hash
        self._simulate_resolve(db, eth, rows, timeout_seconds=600)
        db.commit()
        row = db.execute(sql_text("SELECT status FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "pending"

    def test_not_found_over_timeout_becomes_dropped(self, db):
        import tx_log
        rid = tx_log.record_pending(db, "0xff", "0xu", "0xt", "0x00", "stake")
        db.commit()
        FakeRow = types.SimpleNamespace
        rows = [FakeRow(id=rid, tx_hash="0xff", age_sec=700)]  # past timeout
        eth = FakeWeb3Eth({})
        self._simulate_resolve(db, eth, rows, timeout_seconds=600)
        db.commit()
        row = db.execute(sql_text("SELECT * FROM tx_log WHERE id = :i"), {"i": rid}).first()
        assert row.status == "dropped"


# ── Notifications shape ────────────────────────────────────────────

class TestNotificationsShape:
    def test_pending_and_recent_grouped_by_address(self, db):
        import tx_log
        # Two pending for user A, one confirmed for user A, one for user B
        tx_log.record_pending(db, "0x1", "0xAAA", "0xt", "0x00", "stake")
        tx_log.record_pending(db, "0x2", "0xAAA", "0xt", "0x00", "claim")
        rid3 = tx_log.record_pending(db, "0x3", "0xAAA", "0xt", "0x00", "stake")
        tx_log.record_pending(db, "0x4", "0xBBB", "0xt", "0x00", "stake")
        db.commit()
        tx_log.mark_confirmed(db, rid3, block_number=1, gas_used=1, post_id=42)
        db.commit()

        data = tx_log.get_user_notifications(db, "0xAAA")
        assert data["address"] == "0xaaa"
        assert len(data["pending"]) == 2
        assert len(data["recent"]) == 1
        assert data["recent"][0]["status"] == "confirmed"
        assert data["recent"][0]["post_id"] == 42

        data_b = tx_log.get_user_notifications(db, "0xBBB")
        assert len(data_b["pending"]) == 1
        assert len(data_b["recent"]) == 0

    def test_address_is_case_insensitive(self, db):
        import tx_log
        tx_log.record_pending(db, "0x1", "0xAbC", "0xt", "0x00", "stake")
        db.commit()
        data_lower = tx_log.get_user_notifications(db, "0xabc")
        data_upper = tx_log.get_user_notifications(db, "0xABC")
        assert len(data_lower["pending"]) == 1
        assert len(data_upper["pending"]) == 1
