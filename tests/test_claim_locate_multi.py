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


# patch_locate_check — "Check on Verisphere" from any page: one sentence, no page phrases, the whole corpus as candidates.
def test_check_mode_uses_the_corpus_not_the_page_phrases(monkeypatch):
    calls = {"all": 0, "phr": 0}
    corpus = [(101, "Climate change threatens people with increased flooding."), (104, "The Paris Agreement was adopted in 2015.")]
    monkeypatch.setattr(cl, "_all_claims", lambda db: calls.__setitem__("all", calls["all"] + 1) or corpus)
    monkeypatch.setattr(cl, "_candidate_claims", lambda db, phrases: calls.__setitem__("phr", calls["phr"] + 1) or [])
    monkeypatch.setattr(cl, "_vector_matches", lambda db, texts, pids: {})
    monkeypatch.setattr(cl, "_summary", lambda db, pid: {"post_id": pid, "text": dict(corpus)[pid], "verity_score": 10.0, "is_active": True, "total_stake": 3.0})
    body = {"title": "Some blog post", "url": "https://example.org/x", "phrases": [], "check": True,
            "sentences": [{"sentenceId": "check-1", "text": "Climate change threatens people with increased flooding."}]}
    out = cl.locate_claims(body, None, db=None)
    assert calls == {"all": 1, "phr": 0}
    assert [g["claim"]["post_id"] for g in out["groups"]] == [101] and out["groups"][0]["sentence_ids"] == ["check-1"]
    # without the flag (or with several sentences) the page-phrase path is taken as before
    calls.update(all=0, phr=0)
    cl.locate_claims({**body, "check": False}, None, db=None)
    cl.locate_claims({**body, "sentences": body["sentences"] * 2}, None, db=None)
    assert calls == {"all": 0, "phr": 2}


# patch_locate_anchors — a sentence that leans on its page is matched with the page's anchors
def test_page_anchors_are_short_proper_noun_phrases_from_title_and_phrases():
    a = cl.page_anchors("COVID-19 pandemic - Wikipedia", ["Wuhan", "wuhan", "Huanan Seafood Wholesale Market", "x", "a lowercase phrase", "A" * 80, "Severe acute respiratory syndrome coronavirus 2"])
    assert a[0] == "COVID-19 pandemic" and "Wuhan" in a and "Huanan Seafood Wholesale Market" in a
    assert "x" not in a and "a lowercase phrase" not in a and not any(len(x) > 60 for x in a) and len(a) <= cl.MAX_ANCHORS
    assert cl.page_anchors("", []) == []


def test_a_dangling_sentence_matches_through_the_page_anchors_a_self_contained_one_does_not(monkeypatch):
    corpus = [(101, "COVID-19 was first identified in Wuhan in December 2019."), (104, "The Paris Agreement was adopted in 2015.")]
    monkeypatch.setattr(cl, "_candidate_claims", lambda db, phrases: corpus)
    embedded = {}
    monkeypatch.setattr(cl, "_vector_matches", lambda db, texts, pids: embedded.update({"texts": list(texts)}) or {})
    monkeypatch.setattr(cl, "_summary", lambda db, pid: {"post_id": pid, "text": dict(corpus)[pid], "verity_score": 10.0, "is_active": True, "total_stake": 3.0})
    body = {"title": "COVID-19 pandemic - Wikipedia", "url": "https://en.wikipedia.org/wiki/COVID-19_pandemic", "phrases": ["Wuhan"],
            "sentences": [{"sentenceId": "s1", "text": "It was first identified in Wuhan in December 2019."},
                          {"sentenceId": "s2", "text": "The Paris Agreement was adopted in 2015 by 196 parties."}]}
    out = cl.locate_claims(body, None, db=None)
    # s1 leans on the page ("It"): the anchors supply "covid-19" and the lexical stage finds claim 101;
    # s2 stands alone and is matched exactly as written (claim 104), with no anchors added
    by = {g["claim"]["post_id"]: g["sentence_ids"] for g in out["groups"]}
    assert by == {101: ["s1"], 104: ["s2"]}
    assert out["anchored"] == 1
    assert embedded["texts"][0].startswith("It was first identified in Wuhan in December 2019. [COVID-19 pandemic; Wuhan]")
    assert embedded["texts"][1] == "The Paris Agreement was adopted in 2015 by 196 parties."
    # without anchors (no title, no phrases) the same dangling sentence finds nothing
    out2 = cl.locate_claims({**body, "title": "", "phrases": []}, None, db=None)
    assert {g["claim"]["post_id"]: g["sentence_ids"] for g in out2["groups"]} == {104: ["s2"]} and out2["anchored"] == 0
