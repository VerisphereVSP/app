-- patch_invites: sponsored invitations (v2 step 4a). A player escrows VSP from their own balance behind a code; a
-- newcomer redeems the code into their wallet. Wallet-to-wallet only (sponsor address, redeeming address) — no person,
-- no contact, no IP — which the anonymity rule allows. The plaintext code is never stored, only its sha256.
CREATE TABLE IF NOT EXISTS invite_code (
    id           BIGSERIAL PRIMARY KEY,
    code_hash    TEXT        NOT NULL UNIQUE,
    sponsor      TEXT        NOT NULL,                 -- lowercase 0x address
    amount_wei   NUMERIC(78,0) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    escrow_tx    TEXT,                                 -- transferFrom(sponsor -> escrow) hash
    status       TEXT        NOT NULL DEFAULT 'open',  -- open | redeemed | revoked | expired
    redeemed_by  TEXT,                                 -- lowercase 0x address
    redeemed_at  TIMESTAMPTZ,
    redeem_tx    TEXT,                                 -- transfer(escrow -> redeemer) hash
    closed_at    TIMESTAMPTZ,                          -- revoked / expired
    refund_tx    TEXT                                  -- transfer(escrow -> sponsor) hash
);
CREATE INDEX IF NOT EXISTS idx_invite_code_sponsor_status ON invite_code (sponsor, status);
CREATE INDEX IF NOT EXISTS idx_invite_code_open_expiry ON invite_code (expires_at) WHERE status = 'open';
