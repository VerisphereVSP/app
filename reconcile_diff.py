#!/usr/bin/env python3
"""reconcile_diff.py — chain <-> database reconciliation, READ-ONLY (checklist #12, diff half).

Compares what the indexer believes with what the chain says, post by post:
  posts        : existence, content type, creator, support/challenge totals
  claim text   : on-chain normalized text vs chain_claim_text
  links        : LinkGraph incoming edges of every claim vs chain_link rows
  user stakes  : every (user, post, side) row in chain_user_stake vs StakeEngine.getUserStake
                 (rows the DB has; a user the DB never saw is reported via totals mismatch)

Usage (inside the app container):   python reconcile_diff.py [--posts 1-50] [--tolerance 1e-6]
Exit 0 = no drift; 1 = drift found (lines prefixed DRIFT); 2 = could not run.
Repairs nothing. The repair half is the four-week post-launch item.
"""
from __future__ import annotations
import argparse, sys
from sqlalchemy import text as sql_text
from db import get_session_factory
from chain.chain_reader import _get_stake_engine
from chain.registry_reader import _get_registry, get_claim_text
from chain_indexer import _load_abi  # LinkGraph ABI via the indexer's loader
from config import DEPLOYED
from web3 import Web3

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--posts", default=""); ap.add_argument("--tolerance", type=float, default=1e-6)
    a = ap.parse_args()
    reg = _get_registry(); se = _get_stake_engine(); w3 = se.w3
    lg = w3.eth.contract(address=Web3.to_checksum_address(DEPLOYED["LinkGraph"]), abi=_load_abi("LinkGraph"))
    n = int(reg.functions.nextPostId().call())
    lo, hi = 1, n - 1
    if a.posts:
        lo, hi = [int(x) for x in a.posts.split("-")]; hi = min(hi, n - 1)
    db = get_session_factory()()
    drift = 0; checked = 0
    def D(msg):
        nonlocal drift; drift += 1; print("DRIFT " + msg)
    posts = {r[0]: r for r in db.execute(sql_text("SELECT post_id, content_type, creator, support_total, challenge_total FROM chain_post")).fetchall()}
    links = {r[0]: r for r in db.execute(sql_text("SELECT link_post_id, from_post_id, to_post_id, is_challenge FROM chain_link")).fetchall()}
    for pid in range(lo, hi + 1):
        checked += 1
        try:
            post = reg.functions.getPost(pid).call()
        except Exception as e:
            D(f"post {pid}: chain getPost failed: {e}"); continue
        creator, ctype = post[0], int(post[2])
        sup_w, chal_w = se.functions.getPostTotals(pid).call()
        sup, chal = sup_w / 1e18, chal_w / 1e18
        row = posts.get(pid)
        if row is None:
            D(f"post {pid}: exists on chain (type={ctype}, creator={creator[:10]}) but missing from chain_post"); continue
        if int(row[1]) != ctype: D(f"post {pid}: content_type db={row[1]} chain={ctype}")
        if (row[2] or "").lower() != creator.lower(): D(f"post {pid}: creator db={row[2]} chain={creator}")
        if abs(float(row[3]) - sup) > a.tolerance: D(f"post {pid}: support db={row[3]:.6f} chain={sup:.6f}")
        if abs(float(row[4]) - chal) > a.tolerance: D(f"post {pid}: challenge db={row[4]:.6f} chain={chal:.6f}")
        if ctype == 0:
            ct = get_claim_text(pid)
            dbt = db.execute(sql_text("SELECT claim_text FROM chain_claim_text WHERE post_id=:p"), {"p": pid}).scalar()
            if ct is not None and (dbt or "") != ct: D(f"post {pid}: claim text differs (db {len(dbt or '')} chars, chain {len(ct)} chars)")
            for e in lg.functions.getIncoming(pid).call():
                from_pid, link_pid, is_ch = int(e[0]), int(e[1]), bool(e[2])
                lr = links.get(link_pid)
                if lr is None: D(f"link {link_pid}: {from_pid}->{pid} on chain, missing from chain_link")
                elif (int(lr[1]), int(lr[2]), bool(lr[3])) != (from_pid, pid, is_ch): D(f"link {link_pid}: db=({lr[1]}->{lr[2]},ch={lr[3]}) chain=({from_pid}->{pid},ch={is_ch})")
    for pid, from_pid, to_pid, is_ch in [(r[0], r[1], r[2], r[3]) for r in links.values()]:
        if lo <= int(to_pid) <= hi:
            found = any(int(e[1]) == int(pid) for e in lg.functions.getIncoming(int(to_pid)).call())
            if not found: D(f"link {pid}: in chain_link ({from_pid}->{to_pid}) but not on chain")
    rows = db.execute(sql_text("SELECT user_address, post_id, side, amount FROM chain_user_stake")).fetchall()
    for user, pid, side, amount in rows:
        if not (lo <= int(pid) <= hi): continue
        try:
            ch = se.functions.getUserStake(Web3.to_checksum_address(user), int(pid), int(side)).call() / 1e18
        except Exception as e:
            D(f"stake {user[:10]} post {pid} side {side}: chain read failed: {e}"); continue
        if abs(float(amount) - ch) > a.tolerance: D(f"stake {user[:10]} post {pid} side {side}: db={float(amount):.6f} chain={ch:.6f}")
    print(f"reconcile: posts {lo}-{hi} ({checked} checked), {len(rows)} user-stake rows, {len(links)} links; drift lines: {drift}")
    print("VERDICT: " + ("PASS — database matches chain" if drift == 0 else f"DRIFT — {drift} discrepancy line(s) above; re-index the affected posts (POST /api/reindex/<post>?user=<addr>) or investigate"))
    return 0 if drift == 0 else 1

if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as e:
        print(f"reconcile: could not run: {e}"); sys.exit(2)
