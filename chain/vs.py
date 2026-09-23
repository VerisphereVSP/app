"""Verity Score — the app's single off-chain source (patch_vs_single_source).

The ONLY implementation of a claim's Verity Score is ScoreEngine.effectiveVSRay
on-chain (core/src/ScoreEngine.sol, whitepaper §4.2.3). This module is the one
place the app reads and converts it, and the one place a set of claims is
aggregated into a group score. Nothing else in the app may compute a VS:

  * no stake-share fallback when the RPC read fails — a read failure RAISES,
    it never becomes a made-up score;
  * a group of claims (dupe group, article lane) is the stake-weighted mean of
    its members' chain VS; incoming links play no separate part, because each
    member's chain VS already contains them.
"""
from __future__ import annotations

from typing import Iterable, Tuple

RAY = 10 ** 18
VS_MIN = -100.0
VS_MAX = 100.0


def ray_to_pct(ray_value: int) -> float:
    """effectiveVSRay / baseVSRay are int256 with 1e18 == +100%."""
    return (int(ray_value) / RAY) * 100.0


def clamp_pct(value: float) -> float:
    return max(VS_MIN, min(VS_MAX, float(value)))


def read_effective_vs_pct(score_engine, post_id: int) -> float:
    """ScoreEngine.effectiveVSRay(post_id) as a percentage.

    `score_engine` is a web3 contract object for ScoreEngine. Raises whatever
    the RPC raises; there is deliberately NO fallback formula here.
    """
    return ray_to_pct(score_engine.functions.effectiveVSRay(post_id).call())


def read_base_vs_pct(score_engine, post_id: int) -> float:
    """ScoreEngine.baseVSRay(post_id) as a percentage. Raises on failure."""
    return ray_to_pct(score_engine.functions.baseVSRay(post_id).call())


def stake_weighted_vs(members: Iterable[Tuple[float, float]]) -> float:
    """Group VS = sum(vs_i * stake_i) / sum(stake_i) over (vs_pct, stake) pairs.

    stake is the member's direct stake (support + challenge, VSP units).
    Members with zero stake carry zero weight; a group with no stake reads 0.
    Result is clamped to [-100, 100]. Links are NOT an input: each member's
    vs already includes its incoming evidence.
    """
    num = 0.0
    den = 0.0
    for vs, stake in members:
        s = float(stake)
        if s <= 0.0:
            continue
        num += float(vs) * s
        den += s
    if den <= 0.0:
        return 0.0
    return clamp_pct(num / den)
