# VeriSphere App (FastAPI)

This repo is the **application API** used by the frontend. It exposes the HTTP
surface area for articles, claims, staking, and the market maker. It calls
OpenAI for article generation and sentence cleanup.

## Quickstart

```bash
cd app
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8070
```

## Environment

- `OPENAI_API_KEY` — required for article generation and sentence cleanup.
- `DATABASE_URL` — PostgreSQL connection string.
- `RPC_URL` — Avalanche Fuji RPC endpoint.
- `MM_PRIVATE_KEY` — market maker wallet private key (for relay signing).
- `CHAIN_ID` — defaults to `43113` (Fuji).

See `ops/compose/.env.example` for the full list.

## Migrations

Schema is managed via SQL files in `ops/compose/migrations/`. Run them with:

```bash
python -m migrate        # apply all migrations
python -m migrate reset  # DROP + recreate schema (dev only)
```

## Core Endpoints

### Health

- `GET /healthz` — returns `{"ok":"true"}` when the service is up.

### Articles

- `GET /api/article/{topic}` — get or auto-generate article for a topic.
- `POST /api/article/{topic}/generate` — generate (or regenerate) an article.
- `POST /api/article/sentence/insert` — add a sentence to a section.
- `POST /api/article/sentence/{id}/edit` — replace a sentence (creates new + marks old replaced).
- `POST /api/article/sentence/{id}/link_post` — link a sentence to its on-chain post\_id.
- `POST /api/article/sentence/cleanup` — AI grammar/pronoun cleanup.
- `GET /api/disambiguate?q=...` — typeahead search across articles and claims.

### Claims

- `GET /api/claims/fast/all` — all on-chain claims with metrics (indexed DB, fast).
- `GET /api/claims/search?q=...` — search claims by text.
- `GET /api/claims/{post_id}/summary` — full claim summary from ProtocolViews.
- `GET /api/claims/{post_id}/edges` — incoming and outgoing evidence links.
- `GET /api/claims/{post_id}/stakes?user=0x...` — stake totals and user positions.
- `GET /api/claim-status/{claim_text}` — full claim state including on-chain data.

### Relay (meta-transactions)

- `POST /relay` — submit a signed meta-transaction for gasless execution.
- `GET /relay/nonce/{address}` — get the user's forwarder nonce.

### Market Maker

- `GET /api/mm/quote?side=buy&qty=10` — get a price quote.
- `POST /api/mm/trade` — execute a trade.
- `GET /api/mm/floor` — liquidation floor price.

### Portfolio

- `GET /api/portfolio/{address}` — user's staked positions with APR estimates.

### Contracts

- `GET /api/contracts` — deployed contract addresses.

## Background Processes

Two indexers run at startup:

1. **`chain/indexer.py`** (async) — polls `nextPostId()` and syncs new claims
   into the `claim` and `claim_embedding` tables for semantic search.
2. **`chain_indexer.py`** (thread) — polls contract events and syncs full
   on-chain state into `chain_post`, `chain_user_stake`, `chain_link`, etc.
   for fast API reads.

Both are necessary: the first feeds the semantic/dedup system, the second
feeds the indexed read model.

### Settlement keeper (whitepaper v18 — settlement on stored snapshots)

`settle_keeper.py` runs from the worker once per epoch (default 86400 s, 600 s after the
boundary) and calls `StakeEngine.updatePost` on every post that has not settled this epoch,
in **keeper order**: claims, then the links that hang off them, then the claims those links
point to (`topo_order` over parent → link → child; cycles fall back to id order, which the
contract tolerates with one epoch of lag). A child's settlement reads the snapshots its
parents and links wrote moments earlier in the same pass, so evidence counts the epoch after
it is placed. Idempotent: posts already at the current epoch are skipped.

When a user transaction hits `SettleFirst(postId)` (a counted snapshot older than the
previous epoch, or an unseeded claim right after the upgrade), the relay starts a one-hop
repair from the keeper account — that post's parents, links, then the post — so the user's
retry settles inline. At most once per post per `SETTLE_REPAIR_COOLDOWN_SEC` (60).

Upgrade runbook (core `script/UpgradeSnapshots.s.sol`): the script seeds every existing
post's first snapshot — links first, then claims in `SEED_ORDER`, then any remaining claim
by id. Produce `SEED_ORDER` from the indexed graph so children are seeded after their
parents:

```bash
docker compose --env-file /dev/shm/vsp-resolved.env exec -T app python settle_keeper.py seed-order
```

and pass the printed list as `SEED_ORDER=…` to the upgrade script. Seeding is per-post
idempotent; an unset `SEED_ORDER` is safe (unseeded parents simply count from their next
settlement).
