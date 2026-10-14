-- patch_report_path: operator takedown. A claim lives on chain forever; this hides its text from the app's views
-- (the stake and score stay visible) until an admin unhides it. Reason and timestamps for the audit trail.
ALTER TABLE chain_claim_text ADD COLUMN IF NOT EXISTS admin_hidden BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chain_claim_text ADD COLUMN IF NOT EXISTS admin_hidden_reason TEXT;
ALTER TABLE chain_claim_text ADD COLUMN IF NOT EXISTS admin_hidden_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_chain_claim_text_admin_hidden ON chain_claim_text (post_id) WHERE admin_hidden;
