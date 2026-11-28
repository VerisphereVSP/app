-- patch_visits: where visits come from, as a daily COUNT per source tag and nothing else — no visitor row, no IP, no
-- wallet, no user agent. A source is the short ?src= tag on a link Verisphere itself handed out (fb, x, lw, …); it
-- names a post, never a person. Same principle as funnel_daily.
CREATE TABLE IF NOT EXISTS funnel_src_daily (
    day   DATE    NOT NULL,
    src   TEXT    NOT NULL,
    n     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, src)
);
