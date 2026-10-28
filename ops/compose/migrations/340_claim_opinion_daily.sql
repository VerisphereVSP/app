-- patch_share_interactive: the share page (/c/<id>) lets a reader take a side without a wallet. What is kept is a daily
-- COUNT per claim and side — no per-visitor row, no IP, no wallet, nothing that could be joined to a person
-- (design doc "Principles": no wallet-to-contact link). An opinion is not a stake and never enters settlement.
CREATE TABLE IF NOT EXISTS claim_opinion_daily (
    post_id     BIGINT  NOT NULL,
    day         DATE    NOT NULL,
    support_n   INTEGER NOT NULL DEFAULT 0,
    challenge_n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, day)
);
