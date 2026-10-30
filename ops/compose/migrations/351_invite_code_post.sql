-- patch_invites_2: an invitation may be tied to the claim it was shared from (NULL = any claim), so the sponsor's
-- list can say what the VSP was set aside for. Still wallet-to-wallet; a claim id identifies no person.
ALTER TABLE invite_code ADD COLUMN IF NOT EXISTS post_id BIGINT;
