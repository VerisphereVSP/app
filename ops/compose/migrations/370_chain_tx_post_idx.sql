-- patch_activity: the activity feed reads chain_tx by claim and id ("what happened on my claims since I last looked").
-- An index only; no new column, no new table.
CREATE INDEX IF NOT EXISTS idx_chain_tx_post_id ON chain_tx (post_id, id);
