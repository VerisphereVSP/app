-- patch_relay_gas_fee: the relay quotes and collects its own gas-indexed fee (no contract
-- carries fee logic). tx_log keeps the quote shown to the user, the amount the relay
-- collects, and the collection outcome; relay_fee_log.fee_charged_vsp is filled by the
-- collector for the original action's tx_hash.
ALTER TABLE tx_log ADD COLUMN IF NOT EXISTS fee_quote_vsp   DOUBLE PRECISION;  -- displayed to the user (total)
ALTER TABLE tx_log ADD COLUMN IF NOT EXISTS fee_charge_vsp  DOUBLE PRECISION;  -- relay collects this (quote minus any on-chain Forwarder fee)
ALTER TABLE tx_log ADD COLUMN IF NOT EXISTS fee_status      TEXT NOT NULL DEFAULT 'none'
    CHECK (fee_status IN ('none','pending','collected','waived','failed'));
ALTER TABLE tx_log ADD COLUMN IF NOT EXISTS fee_tx_hash     TEXT;
ALTER TABLE tx_log ADD COLUMN IF NOT EXISTS fee_attempts    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tx_log ADD COLUMN IF NOT EXISTS fee_error       TEXT;
CREATE INDEX IF NOT EXISTS idx_tx_log_fee_due ON tx_log (fee_status, status) WHERE fee_status = 'pending';
