"""
unified_grouping.py — single source of truth for dupe grouping, shared by the
Claims view and the Article view so both show IDENTICAL grouping.

PURE MODULE: no DB, no network. Similarity and LLM-equivalence are injected as
callables, so this logic is deterministic and unit-testable. Callers (article
assembler, claims endpoint) provide the real embedding-similarity and the real
LLM verifier; tests provide fakes.

Rules (authoritative — see DESIGN-unified-dedup.md):
  R1. Claims render green (narrative) or red (disputed) by lane; they carry
      stake/VS. Sentences render neutral; they never carry stake/VS.
  R2. Sentences NEVER appear in the disputed lane.
  R3. A claim is NEVER rolled up under a sentence. If a group contains any
      claim, the canonical is a claim — the top-EFFECT one, effect = (support +
      challenge) x |VS| / 100, raw stake as tiebreak (ruling 2026-07-06; matches
      _refresh_group_stats, which re-elects every sweep cycle).
  R4. Grouping is precision-first: exact text always groups; cosine >= HIGH
      auto-groups; [LOW, HIGH) defers to the injected LLM verifier; < LOW never
      groups. (Opposites-with-shared-vocabulary land in the LLM band and are
      correctly split.)

Lane / visibility:
  - claim-anchored group: lane = narrative if aggregate VS > 0 else disputed;
    a group whose claims are entirely unstaked AND aggregate VS == 0 is hidden.
  - sentence-only group: lane = narrative ALWAYS (R2); always visible (prose).
  - non-canonical members are HIDDEN, never deleted (preserves links/history and
    lets a sentence resurface if its anchoring claim later disappears).

Section placement is intentionally NOT handled here — it is an article-layout
concern (heading matching) belonging to the article assembler, not shared
grouping. This module yields lane + canonical + members; the article view maps
the group to a section, the claims view ignores section entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

# Default thresholds (mirror dupe_groups.py after the precision patch).
HIGH_THRESHOLD = 0.95
LOW_THRESHOLD = 0.65
STAKE_EPS = 1e-9

KIND_CLAIM = "claim"
KIND_SENTENCE = "sentence"

LANE_NARRATIVE = "narrative"
LANE_DISPUTED = "disputed"


@dataclass
class Item:
    """A groupable unit: an on-chain claim OR an off-chain sentence."""
    kind: str                       # KIND_CLAIM | KIND_SENTENCE
    id: int                         # post_id (claim) | sentence_id (sentence)
    text: str
    embedding: Optional[Sequence[float]] = None
    stake: float = 0.0              # claims only (support+challenge); 0 for sentences
    vs: float = 0.0                 # claims only, -100..100; 0 for sentences
    post_id: Optional[int] = None   # a sentence may be linked to a claim's post

    @property
    def is_claim(self) -> bool:
        return self.kind == KIND_CLAIM


@dataclass
class Group:
    canonical: Item
    members: List[Item]             # includes canonical
    lane: str                       # LANE_NARRATIVE | LANE_DISPUTED
    claim_anchored: bool
    visible: bool
    agg_stake: float
    agg_vs: float

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def hidden(self) -> List[Item]:
        return [m for m in self.members if m.id is not self.canonical.id or m.kind != self.canonical.kind]

    def hidden_members(self) -> List[Item]:
        """Members other than the canonical (identity = (kind, id))."""
        ck = (self.canonical.kind, self.canonical.id)
        return [m for m in self.members if (m.kind, m.id) != ck]


def _norm(t: str) -> str:
    return (t or "").lower().strip()


def _bundle(a: Item, b: Item, similar, equivalent) -> bool:
    """Precision-first bundle decision between two items (R4)."""
    if _norm(a.text) and _norm(a.text) == _norm(b.text):
        return True  # exact dupe always groups (folds in concept-2 exact weeding)
    if a.embedding is None or b.embedding is None:
        return False
    sim = float(similar(a.embedding, b.embedding))
    if sim >= HIGH_THRESHOLD:
        return True
    if sim < LOW_THRESHOLD:
        return False
    # verification band — ask the injected verifier (LLM in prod)
    return bool(equivalent(a, b, sim))


class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _pick_canonical(members: List[Item]) -> Item:
    """R3: a claim is never rolled under a sentence.
    claim-anchored -> top-EFFECT claim, effect = stake x |vs| / 100 (ruling
    2026-07-06, matching _refresh_group_stats); raw stake breaks ties — which
    also subsumes the all-effects-zero fallback — then lowest id.
    sentence-only  -> most complete sentence (longest text, then lowest id)."""
    claims = [m for m in members if m.is_claim]
    if claims:
        return sorted(claims, key=lambda c: (-(c.stake * abs(c.vs) / 100.0), -c.stake, c.id))[0]
    return sorted(members, key=lambda s: (-len(s.text or ""), s.id))[0]


def _lane_and_visibility(members: List[Item], claim_anchored: bool):
    """Compute (lane, visible, agg_stake, agg_vs) per the rules."""
    if not claim_anchored:
        # sentence-only: always narrative (R2), always visible (prose), no metrics
        return LANE_NARRATIVE, True, 0.0, 0.0

    claims = [m for m in members if m.is_claim]
    agg_stake = sum(c.stake for c in claims)
    # patch_vs_single_source: one aggregator for every group of claims. Unstaked
    # members carry zero weight, so an unstaked group reads 0 (no simple-mean
    # fallback) and is hidden by the rule below.
    from chain.vs import stake_weighted_vs
    agg_vs = stake_weighted_vs((c.vs, c.stake) for c in claims)

    # whole group unstaked AND no VS signal -> hidden
    if agg_stake <= STAKE_EPS and abs(agg_vs) < 1e-9:
        return LANE_NARRATIVE, False, agg_stake, agg_vs

    lane = LANE_NARRATIVE if agg_vs > 0 else LANE_DISPUTED
    return lane, True, agg_stake, agg_vs


def group_items(
    items: Sequence[Item],
    *,
    similar: Callable[[Sequence[float], Sequence[float]], float],
    equivalent: Callable[[Item, Item, float], bool],
) -> List[Group]:
    """Group claims+sentences into dupe groups and resolve canonical/lane/visibility.

    `similar(emb_a, emb_b) -> cosine similarity in [0,1]`
    `equivalent(item_a, item_b, sim) -> bool`  (only called in the [LOW,HIGH) band)

    Deterministic and order-stable: connected components via union-find over
    bundle-worthy pairs, then per-component canonical/lane resolution.
    """
    items = list(items)
    n = len(items)
    dsu = _DSU(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _bundle(items[i], items[j], similar, equivalent):
                dsu.union(i, j)

    # gather components
    comps: dict[int, List[Item]] = {}
    for idx, it in enumerate(items):
        comps.setdefault(dsu.find(idx), []).append(it)

    groups: List[Group] = []
    for members in comps.values():
        claim_anchored = any(m.is_claim for m in members)
        canonical = _pick_canonical(members)
        lane, visible, agg_stake, agg_vs = _lane_and_visibility(members, claim_anchored)
        groups.append(Group(
            canonical=canonical,
            members=members,
            lane=lane,
            claim_anchored=claim_anchored,
            visible=visible,
            agg_stake=agg_stake,
            agg_vs=agg_vs,
        ))

    # stable output order: visible first, then narrative before disputed,
    # then higher aggregate stake, then canonical id.
    groups.sort(key=lambda g: (
        not g.visible,
        g.lane == LANE_DISPUTED,
        -g.agg_stake,
        g.canonical.id,
    ))
    return groups
