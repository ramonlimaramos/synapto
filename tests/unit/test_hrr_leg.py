"""Tests for the HRR leg of the hybrid ranker (#103).

Two defects are pinned here. The old boost added ``((sim + 1) / 2) × 0.15`` to
the RRF sum, so an unrelated memory gained +0.075 against a largest possible
RRF sum of ≈0.033 and the quality weight, not the match, decided the order in
the candidate window. And it compared ``encode_text(query)`` against a memory
stored as ``encode_fact(content, entities)`` — an unbound probe against a
role-bound vector — which is noise by construction, so even the scale fix
alone would have fused nothing.

The contract now: nothing below the noise floor, at most one RRF leg
(``1 / (k + 1)``) for a perfect match, the probe encoded as ``remember``
encodes the memory. Pure functions, no database; the golden set in
``tests/eval`` measures the effect on retrieval.
"""

from __future__ import annotations

import pytest

from synapto.graph.entities import extract_entities_from_text
from synapto.hrr.core import DEFAULT_DIM, encode_fact, encode_text, phases_to_bytes, similarity
from synapto.search.hybrid import DEFAULT_RRF_K, _hrr_evidence, _hrr_leg, _rank_candidates

CONTENT = "the deploy pipeline requires a signed tag before it publishes"
UNRELATED = "quarterly invoices are archived to cold storage after seven years"
ONE_LEG = 1.0 / (DEFAULT_RRF_K + 1)


def stored(content: str, dim: int = DEFAULT_DIM) -> bytes:
    """Encode ``content`` the way ``remember`` does before writing ``hrr_vector``."""
    return phases_to_bytes(encode_fact(content, extract_entities_from_text(content), dim))


class TestEvidence:
    def test_a_memory_without_a_vector_is_no_evidence(self):
        assert _hrr_evidence(CONTENT, None, {}) == 0.0
        assert _hrr_evidence(CONTENT, b"", {}) == 0.0

    def test_an_identical_memory_is_full_evidence(self):
        assert _hrr_evidence(CONTENT, stored(CONTENT), {}) == pytest.approx(1.0)

    def test_an_unrelated_memory_is_no_evidence(self):
        assert _hrr_evidence(UNRELATED, stored(CONTENT), {}) == 0.0

    def test_a_partial_match_lies_strictly_between(self):
        evidence = _hrr_evidence("signed tag before publish", stored(CONTENT), {})

        assert 0.0 < evidence < 1.0

    def test_the_probe_is_role_bound_like_the_memory(self):
        """The regression: an unbound probe against a stored fact reads as noise."""
        unbound = similarity(encode_text(CONTENT), encode_fact(CONTENT, []))

        assert abs(unbound) < 0.1
        assert _hrr_evidence(CONTENT, stored(CONTENT), {}) == pytest.approx(1.0)

    def test_the_probe_is_encoded_once_per_dimension(self):
        probes: dict = {}

        _hrr_evidence(CONTENT, stored(CONTENT), probes)
        _hrr_evidence(CONTENT, stored(UNRELATED), probes)
        _hrr_evidence(CONTENT, stored(CONTENT, dim=512), probes)

        assert sorted(probes) == [512, DEFAULT_DIM]


class TestLeg:
    def test_a_perfect_match_gains_exactly_one_leg(self):
        assert _hrr_leg([{"hrr_vector": stored(CONTENT)}], CONTENT, DEFAULT_RRF_K) == [pytest.approx(ONE_LEG)]

    def test_an_unrelated_memory_gains_nothing(self):
        """Under the old boost this row gained +0.075, more than both SQL legs together."""
        assert _hrr_leg([{"hrr_vector": stored(CONTENT)}], UNRELATED, DEFAULT_RRF_K) == [0.0]

    def test_a_memory_without_a_vector_gains_nothing(self):
        assert _hrr_leg([{"rrf_score": 0.1}, {"hrr_vector": None}], CONTENT, DEFAULT_RRF_K) == [0.0, 0.0]

    def test_no_row_gains_more_than_one_leg(self):
        rows = [{"hrr_vector": stored(text)} for text in (CONTENT, UNRELATED, "signed tag", "deploy pipeline tag")]

        assert max(_hrr_leg(rows, CONTENT, DEFAULT_RRF_K)) <= ONE_LEG

    def test_the_leg_uses_the_callers_k(self):
        assert _hrr_leg([{"hrr_vector": stored(CONTENT)}], CONTENT, rrf_k=10) == [pytest.approx(1.0 / 11)]

    def test_contributions_follow_row_order(self):
        rows = [{"hrr_vector": stored(UNRELATED)}, {"hrr_vector": stored(CONTENT)}]

        unrelated, related = _hrr_leg(rows, CONTENT, DEFAULT_RRF_K)

        assert unrelated == 0.0
        assert related == pytest.approx(ONE_LEG)


class TestRankCandidates:
    def test_the_leg_reaches_the_final_order(self):
        """Equal RRF and weight: the HRR match decides, and by no more than one leg."""
        related = {"rrf_score": ONE_LEG, "quality_weight": 1.0, "hrr_vector": stored(CONTENT), "id": "related"}
        unrelated = {"rrf_score": ONE_LEG, "quality_weight": 1.0, "hrr_vector": stored(UNRELATED), "id": "unrelated"}

        ranked = _rank_candidates([unrelated, related], CONTENT, limit=2)

        assert [row["id"] for row, _ in ranked] == ["related", "unrelated"]
        assert [score for _, score in ranked] == [pytest.approx(2 * ONE_LEG), pytest.approx(ONE_LEG)]

    def test_an_unrelated_match_cannot_outrank_a_better_rrf(self):
        """The old floor (+0.075 × 1.5) let a stale core memory beat any keyword match; a zero cannot."""
        stale_core = {"rrf_score": 1 / 61, "quality_weight": 1.5, "hrr_vector": stored(UNRELATED), "id": "core"}
        matching = {"rrf_score": 2 / 61, "quality_weight": 1.0, "hrr_vector": None, "id": "match"}

        ranked = _rank_candidates([stale_core, matching], CONTENT, limit=2)

        assert [row["id"] for row, _ in ranked] == ["match", "core"]

    def test_k_is_shared_with_the_sql_legs(self):
        ranked = _rank_candidates([{"rrf_score": 0.0, "hrr_vector": stored(CONTENT)}], CONTENT, limit=1, rrf_k=10)

        assert ranked[0][1] == pytest.approx(1.0 / 11)
