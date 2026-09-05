"""Tests that decay, trust and layer weight reach the *final* order of a recall.

Until 0.7.0 they did not. The SQL ordered candidates by ``rrf × weight`` and
then returned only the raw ``rrf_score``; the Python re-rank sorted on that
plus the HRR boost, so the three quality signals decided who entered the
candidate list and nothing else. Every existing search test asserted filters,
never relative position, which is why it went unnoticed.

The database tests hold relevance constant by inserting the *same content*
twice — identical embedding, identical ``tsv``, therefore identical RRF — and
varying one quality signal at a time. If the signal reaches the final order,
the pair comes back in a known sequence.

Ties alone cannot expose the old bug: a stable sort on equal RRF preserves the
SQL's weighted order by accident. So one test gives the *low-quality* twin a
slightly better match — a verbatim copy of the query prepended — and asserts
that the weight still wins. That is the test that failed against 0.7.0.
"""

from __future__ import annotations

import pytest

from synapto.search.hybrid import DEPTH_BOOST, RRF_QUERY_TEMPLATE, _rank_candidates, hybrid_search

TENANT = "acme/ranking-weights"
CONTENT = "the deploy pipeline requires a signed tag before it publishes"


class TestRankCandidates:
    """Pure-function checks on the formula, no database."""

    def test_the_weight_multiplies_the_relevance(self):
        rows = [{"rrf_score": 0.5, "quality_weight": 0.5}, {"rrf_score": 0.4, "quality_weight": 1.5}]

        ranked = _rank_candidates(rows, "q", limit=2)

        assert [score for _, score in ranked] == pytest.approx([0.6, 0.25])

    def test_a_higher_rrf_loses_to_a_higher_weight(self):
        """The regression: sorting by raw RRF would put the low-quality row first."""
        low_quality = {"rrf_score": 0.9, "quality_weight": 0.5, "id": "low"}
        high_quality = {"rrf_score": 0.7, "quality_weight": 1.5, "id": "high"}

        ranked = _rank_candidates([low_quality, high_quality], "q", limit=2)

        assert [row["id"] for row, _ in ranked] == ["high", "low"]

    def test_the_cut_happens_after_weighting(self):
        strong_but_stale = {"rrf_score": 1.0, "quality_weight": 0.1, "id": "a"}
        weak_but_sound = {"rrf_score": 0.2, "quality_weight": 1.0, "id": "b"}

        ranked = _rank_candidates([strong_but_stale, weak_but_sound], "q", limit=1)

        assert [row["id"] for row, _ in ranked] == ["b"]

    def test_a_row_without_a_weight_weighs_one(self):
        ranked = _rank_candidates([{"rrf_score": 0.3}], "q", limit=1)

        assert ranked[0][1] == pytest.approx(0.3)


class TestTheSqlAndPythonAgreeOnLayerWeights:
    """The template is static SQL; ``DEPTH_BOOST`` mirrors it. This is the only thing keeping them honest."""

    @pytest.mark.parametrize(("layer", "weight"), list(DEPTH_BOOST.items()))
    def test_every_python_weight_is_spelled_out_in_the_sql(self, layer, weight):
        assert RRF_QUERY_TEMPLATE.count(f"WHEN '{layer}' THEN {weight}") == 2, "once to select, once to order"

    def test_the_sql_names_no_layer_python_does_not_know(self):
        arms = {line.split("'")[1] for line in RRF_QUERY_TEMPLATE.splitlines() if "WHEN '" in line}

        assert arms == set(DEPTH_BOOST)

    def test_the_template_selects_the_weight(self):
        assert "AS quality_weight" in RRF_QUERY_TEMPLATE


@pytest.fixture
async def store(pg, provider):
    await _cleanup(pg)
    yield pg
    await _cleanup(pg)


async def _cleanup(pg):
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))


QUERY = "signed tag before publish"
SLIGHTLY_BETTER_MATCH = f"{QUERY}: {CONTENT}"


async def _insert(pg, provider, *, content=CONTENT, depth_layer="working", trust_score=0.5, decay_score=1.0) -> str:
    embedding = await provider.embed_one(content)
    row = await pg.execute_one(
        """
        INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, trust_score, decay_score)
        VALUES (%s, %s, %s, 'general', %s, %s, %s, %s) RETURNING id;
        """,
        (content, embedding, provider.dimension, TENANT, depth_layer, trust_score, decay_score),
    )
    return str(row["id"])


async def _order(pg, provider) -> list[str]:
    results = await hybrid_search(pg, provider, QUERY, tenant=TENANT, limit=10)
    return [str(r.id) for r in results]


class TestQualitySignalsReachTheFinalOrder:
    async def test_core_outranks_working_at_equal_relevance(self, store, provider):
        working = await _insert(store, provider, depth_layer="working")
        core = await _insert(store, provider, depth_layer="core")

        assert await _order(store, provider) == [core, working]

    async def test_ephemeral_sinks_below_working(self, store, provider):
        ephemeral = await _insert(store, provider, depth_layer="ephemeral")
        working = await _insert(store, provider, depth_layer="working")

        assert await _order(store, provider) == [working, ephemeral]

    async def test_a_downvoted_memory_ranks_below_its_twin(self, store, provider):
        distrusted = await _insert(store, provider, trust_score=0.2)
        trusted = await _insert(store, provider, trust_score=0.8)

        assert await _order(store, provider) == [trusted, distrusted]

    async def test_a_decayed_memory_ranks_below_its_twin(self, store, provider):
        decayed = await _insert(store, provider, decay_score=0.3)
        fresh = await _insert(store, provider, decay_score=1.0)

        assert await _order(store, provider) == [fresh, decayed]

    async def test_a_core_memory_beats_a_slightly_better_matching_ephemeral_one(self, store, provider):
        """The 0.7.0 regression, reproduced: raw RRF favours the ephemeral twin."""
        ephemeral = await _insert(store, provider, content=SLIGHTLY_BETTER_MATCH, depth_layer="ephemeral")
        core = await _insert(store, provider, depth_layer="core")

        assert await _order(store, provider) == [core, ephemeral]

    async def test_the_slightly_better_match_really_is_more_relevant(self, store, provider):
        """Guards the test above: at equal weight, the better match must win."""
        better = await _insert(store, provider, content=SLIGHTLY_BETTER_MATCH)
        plain = await _insert(store, provider)

        assert await _order(store, provider) == [better, plain]

    async def test_the_reported_score_carries_the_weight(self, store, provider):
        await _insert(store, provider, depth_layer="core")
        await _insert(store, provider, depth_layer="ephemeral")

        first, second = await hybrid_search(store, provider, QUERY, tenant=TENANT, limit=10)

        assert first.rrf_score == pytest.approx(second.rrf_score * DEPTH_BOOST["core"] / DEPTH_BOOST["ephemeral"])

    async def test_insertion_order_does_not_decide(self, store, provider):
        """Same pair, reversed insertion — the weight must still win the tie."""
        core = await _insert(store, provider, depth_layer="core")
        working = await _insert(store, provider, depth_layer="working")

        assert await _order(store, provider) == [core, working]
