"""claim_locate: a sentence that states several claims resolves to all of them.

"Climate change threatens people with increased flooding, extreme heat, …" used to
resolve to ONE claim — whichever of the flooding and heat claims the candidate
query happened to list first. Both stages now keep every claim that clears the
threshold (best first, capped), generic claims subsumed by a specific match are
dropped, and the endpoint puts the sentence in each claim's group.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claim_locate as cl  # noqa: E402

SENTENCE = ("Climate change threatens people with increased flooding, extreme heat, "
            "increased food and water scarcity, more disease, and economic loss.")
FLOOD = (101, cl._tokens("Climate change threatens people with increased flooding."))
HEAT = (102, cl._tokens("Climate change threatens people with extreme heat."))
GENERIC = (103, cl._tokens("Climate change threatens people."))          # 4 tokens: just clears the floor
UNRELATED = (104, cl._tokens("The Paris Agreement was adopted in 2015."))


def test_two_claims_in_one_sentence_are_both_returned():
    sent = set(cl._tokens(SENTENCE))
    out = cl._lexical_matches([sent], [FLOOD, HEAT, UNRELATED])
    assert sorted(pid for pid, _ in out[0]) == [101, 102]
    assert all(score == 1.0 for _, score in out[0])


def test_order_does_not_decide_the_winner_any_more():
    sent = set(cl._tokens(SENTENCE))
    a = cl._lexical_matches([sent], [FLOOD, HEAT])[0]
    b = cl._lexical_matches([sent], [HEAT, FLOOD])[0]
    assert sorted(a) == sorted(b)


def test_generic_claim_subsumed_by_a_specific_match_is_dropped():
    sent = set(cl._tokens(SENTENCE))
    out = cl._lexical_matches([sent], [GENERIC, FLOOD, HEAT])
    assert sorted(pid for pid, _ in out[0]) == [101, 102]
    # … but it stays when it is the only thing the sentence states
    alone = cl._lexical_matches([set(cl._tokens("Climate change threatens people everywhere."))], [GENERIC, FLOOD])
    assert [pid for pid, _ in alone[0]] == [103]


def test_single_claim_sentences_are_unchanged():
    sent = set(cl._tokens("Climate change threatens people with extreme heat in cities."))
    out = cl._lexical_matches([sent], [FLOOD, HEAT, UNRELATED])
    assert out == {0: [(102, 1.0)]}
    assert cl._lexical_matches([set(cl._tokens("Nothing to see here at all."))], [FLOOD, HEAT]) == {}


def test_per_sentence_cap_keeps_the_best():
    sent = set(cl._tokens(SENTENCE))
    many = [(200 + i, cl._tokens(f"Climate change threatens people with increased flooding {w}"))
            for i, w in enumerate(["extreme", "heat", "disease", "economic", "water", "scarcity"])]
    out = cl._lexical_matches([sent], many, limit=3)
    assert len(out[0]) == 3


def test_merge_takes_the_more_confident_score_and_unions_the_stages():
    lexical = {0: [(101, 1.0)], 1: [(104, 0.9)]}
    vector = {0: [(102, 0.93), (101, 0.85)], 2: [(105, 0.81)]}
    out = cl._merge_matches(lexical, vector, limit=4)
    assert out[0] == [(101, 1.0), (102, 0.93)]
    assert out[1] == [(104, 0.9)]
    assert out[2] == [(105, 0.81)]
    assert cl._merge_matches({0: [(1, 0.9), (2, 0.8), (3, 0.7)]}, {}, limit=2)[0] == [(1, 0.9), (2, 0.8)]
