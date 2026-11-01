-- patch_funnel: how readers move from a share link to a first stake, as a daily COUNT per step and nothing else —
-- no visitor row, no IP, no wallet, no user agent (design doc "Principles": no wallet-to-contact link). The steps are a
-- fixed list in app/funnel.py; the share page, the opinion and invitation routes count server-side, the Start sheet
-- posts its steps from the browser.
CREATE TABLE IF NOT EXISTS funnel_daily (
    day   DATE    NOT NULL,
    step  TEXT    NOT NULL,
    n     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, step)
);
