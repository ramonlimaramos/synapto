"""The ablation runner and the contracts that make its numbers trustworthy.

Two kinds of test. The runner itself is opt-in (``SYNAPTO_EVAL_ABLATION=1``):
it seeds the corpus, measures every configuration, reseeds five times in
shuffled order for the noise floor, and writes ``docs/eval/ablation.md``. It
is not part of the default run because it takes tens of seconds and its
output is a committed document, not a pass/fail.

Everything else runs always and needs no flag: the configuration table is what
issue #94 asked for, every variant is executable SQL, switching a signal off
really removes it from the score (asserted on the reported score, not on the
patched call), and the arithmetic behind the floor and the verdicts is pinned.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from synapto.db.migrations import run_migrations
from synapto.hrr.core import DEFAULT_DIM, encode_fact, phases_to_bytes
from synapto.search.hybrid import hybrid_search
from synapto.sql.search import RRF_QUERY_TEMPLATE
from tests.eval import ablation, harness, seeding
from tests.eval.ablation import (
    BLOCKS,
    CONDITIONAL,
    CONFIGURATIONS,
    FULL,
    NO_HRR,
    NOISE_FLOOR_RUNS,
    PRIMARY,
    RRF_ONLY,
    AblationError,
    Block,
    Configuration,
    Verdict,
)
from tests.eval.harness import OVERALL, SIGNALS, Metrics

ABLATION_FLAG = "SYNAPTO_EVAL_ABLATION"
REPORT_PATH = harness.EVAL_DIR.parent.parent / "docs" / "eval" / "ablation.md"
BY_NAME = {configuration.name: configuration for configuration in CONFIGURATIONS}
FLAGS = ("hrr", "decay", "trust", "layer", "vector_leg", "keyword_leg")

TENANT = "acme/ablation"
CONTENT = "the deploy pipeline requires a signed tag before it publishes"
QUERY = "signed tag before publish"
RRF_K = 60
SINGLE_LEG_RRF = 1 / (RRF_K + 1)
DEFAULT_TRUST = 0.5


class TestConfigurations:
    def test_the_primary_block_is_the_eight_configurations_the_issue_names(self):
        assert [c.name for c in (*PRIMARY.configurations, RRF_ONLY)] == [
            "full",
            "no-hrr",
            "no-decay",
            "no-trust",
            "no-layer",
            "keyword-only",
            "vector-only",
            "rrf-only",
        ]

    def test_full_has_every_signal_on(self):
        assert FULL == Configuration("full")

    def test_names_are_unique(self):
        names = [c.name for c in CONFIGURATIONS]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize(("block", "signal"), [(b, s) for b in BLOCKS for s in b.ablated_by_signal])
    def test_each_ablation_removes_exactly_one_flag_from_its_reference(self, block, signal):
        configuration = block.ablated_by_signal[signal]
        removed = [flag for flag in FLAGS if getattr(block.reference, flag) and not getattr(configuration, flag)]
        restored = [flag for flag in FLAGS if not getattr(block.reference, flag) and getattr(configuration, flag)]
        assert len(removed) == 1, (block.title, signal, removed)
        assert restored == [], (block.title, signal, restored)

    def test_the_conditional_block_starts_without_hrr_and_skips_the_hrr_signal(self):
        assert CONDITIONAL.reference is NO_HRR
        assert "hrr" not in CONDITIONAL.ablated_by_signal
        assert set(CONDITIONAL.ablated_by_signal) == set(PRIMARY.ablated_by_signal) - {"hrr"}

    def test_rrf_only_keeps_both_legs_and_nothing_else(self):
        assert RRF_ONLY == Configuration("rrf-only", hrr=False, decay=False, trust=False, layer=False)


class TestVariant:
    def test_full_is_the_production_statement_verbatim(self):
        assert ablation.variant(FULL) == RRF_QUERY_TEMPLATE

    def test_no_layer_drops_the_case_arms_and_keeps_decay_and_trust(self):
        statement = ablation.variant(BY_NAME["no-layer"])
        assert "WHEN 'core'" not in statement
        assert statement.count("m.decay_score * m.trust_score AS quality_weight") == 1

    def test_rrf_only_weighs_every_row_one(self):
        statement = ablation.variant(BY_NAME["rrf-only"])
        assert statement.count("1.0 AS quality_weight") == 1
        assert "m.decay_score *" not in statement

    def test_keyword_only_disables_the_vector_leg_alone(self):
        statement = ablation.variant(BY_NAME["keyword-only"])
        vector_leg, keyword_leg = statement.split("keyword_search AS")
        assert "AND false" in vector_leg
        assert "AND false" not in keyword_leg
        assert "{{filters}}" in vector_leg, "the filter slot survives so scoped cases still apply"

    def test_vector_only_disables_the_keyword_leg_alone(self):
        statement = ablation.variant(BY_NAME["vector-only"])
        vector_leg, keyword_leg = statement.split("keyword_search AS")
        assert "AND false" not in vector_leg
        assert "AND false" in keyword_leg
        assert "tsv @@" not in keyword_leg

    def test_every_variant_still_formats_like_the_production_statement(self):
        for configuration in CONFIGURATIONS:
            ablation.variant(configuration).format(dim=384).format(filters="")

    def test_a_drifted_template_is_refused(self):
        with pytest.raises(AblationError, match="production statement changed"):
            ablation.variant(BY_NAME["no-trust"], template="SELECT 1;")


@pytest.fixture
async def twins(pg, provider):
    """Two identical memories, ``core`` then ``ephemeral``, so relevance is constant and only weight varies.

    Both carry an HRR vector, as a memory stored through ``remember`` would,
    so ``full`` really does add a boost that ``no-hrr`` must remove.
    """
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))
    embedding = await provider.embed_one(CONTENT)
    hrr_vector = phases_to_bytes(encode_fact(CONTENT, []))
    ids = []
    for layer in ("core", "ephemeral"):
        row = await pg.execute_one(
            """
            INSERT INTO memories (content, embedding, embedding_dim, type, tenant, depth_layer, hrr_vector, hrr_dim)
            VALUES (%s, %s, %s, 'general', %s, %s, %s, %s) RETURNING id;
            """,
            (CONTENT, embedding, provider.dimension, TENANT, layer, hrr_vector, DEFAULT_DIM),
        )
        ids.append(row["id"])
    yield tuple(ids)
    await pg.execute("DELETE FROM memories WHERE tenant = %s;", (TENANT,))


async def _scores(pg, provider, configuration: Configuration) -> dict:
    with ablation.applied(configuration):
        results = await hybrid_search(pg, provider, QUERY, tenant=TENANT, limit=10)
    return {result.id: result.rrf_score for result in results}


class TestSwitchingASignalOffReachesTheScore:
    @pytest.mark.parametrize("configuration", CONFIGURATIONS, ids=lambda c: c.name)
    async def test_every_configuration_executes_and_returns_both_twins(self, pg, provider, twins, configuration):
        assert set(await _scores(pg, provider, configuration)) == set(twins)

    async def test_no_hrr_leaves_the_bare_weighted_rrf(self, pg, provider, twins):
        """Both legs rank the core twin first: ``2/(k+1) × 0.5 trust × 1.5 core``, and not a boost more."""
        core, _ = twins
        assert (await _scores(pg, provider, BY_NAME["no-hrr"]))[core] == pytest.approx(
            2 * SINGLE_LEG_RRF * DEFAULT_TRUST * 1.5
        )
        assert (await _scores(pg, provider, FULL))[core] > 2 * SINGLE_LEG_RRF * DEFAULT_TRUST * 1.5

    async def test_no_layer_makes_the_twins_tie(self, pg, provider, twins):
        core, ephemeral = twins
        scores = await _scores(pg, provider, BY_NAME["no-layer"])
        assert scores[core] == pytest.approx(scores[ephemeral])
        full = await _scores(pg, provider, FULL)
        assert full[core] == pytest.approx(full[ephemeral] * 3)

    async def test_rrf_only_reports_the_unweighted_sum(self, pg, provider, twins):
        core, ephemeral = twins
        scores = await _scores(pg, provider, BY_NAME["rrf-only"])
        assert scores[core] == pytest.approx(2 * SINGLE_LEG_RRF), "identical content ties both legs at rank 1"
        assert scores[ephemeral] == pytest.approx(2 * SINGLE_LEG_RRF)

    async def test_a_single_leg_contributes_one_reciprocal_rank(self, pg, provider, twins):
        core, _ = twins
        rrf_only_one_leg = Configuration("probe", hrr=False, decay=False, trust=False, layer=False, keyword_leg=False)
        assert (await _scores(pg, provider, rrf_only_one_leg))[core] == pytest.approx(SINGLE_LEG_RRF)

    async def test_the_patch_does_not_outlive_the_block(self, pg, provider, twins):
        await _scores(pg, provider, BY_NAME["rrf-only"])
        core, ephemeral = twins
        after = await _scores(pg, provider, FULL)
        assert after[core] == pytest.approx(after[ephemeral] * 3)


class TestArithmetic:
    def test_shuffled_is_reproducible_and_a_permutation(self):
        corpus = harness.load_corpus()
        first, again, other = ablation.shuffled(corpus, 1), ablation.shuffled(corpus, 1), ablation.shuffled(corpus, 2)
        assert first == again
        assert first != other
        assert sorted(m.key for m in first) == sorted(m.key for m in corpus)

    def test_noise_floor_is_the_spread_of_overall_mrr(self):
        runs = [{OVERALL: Metrics(0.50, 1.0, 1)}, {OVERALL: Metrics(0.53, 1.0, 1)}, {OVERALL: Metrics(0.51, 1.0, 1)}]
        assert ablation.noise_floor(runs) == pytest.approx(0.03)

    @pytest.mark.parametrize(
        ("delta", "label", "follow_up"),
        [(-0.10, "earns its place", False), (0.01, "below the noise floor", True), (0.10, "hurts retrieval", True)],
    )
    def test_verdict_labels(self, delta, label, follow_up):
        verdict = Verdict("hrr", "no-hrr", delta, floor=0.02)
        assert verdict.label == label
        assert verdict.needs_follow_up is follow_up

    def test_the_floor_is_inclusive(self):
        assert Verdict("hrr", "no-hrr", -0.02, floor=0.02).label == "below the noise floor"

    def test_judge_measures_each_signal_against_the_block_reference(self):
        results = _results(full=0.5, **{"no-hrr": 0.9, "no-decay": 0.5, "no-trust": 0.4})
        by_signal = {verdict.signal: verdict for verdict in ablation.judge(PRIMARY, results, floor=0.05)}
        assert by_signal["hrr"].delta == pytest.approx(0.4)
        assert by_signal["hrr"].label == "hurts retrieval"
        assert by_signal["decay"].label == "below the noise floor"
        assert by_signal["trust"].label == "earns its place"

    def test_the_conditional_block_compares_with_no_hrr_not_full(self):
        results = _results(full=0.5, **{"no-hrr": 0.9, "no-hrr-no-layer": 0.7})
        by_signal = {verdict.signal: verdict for verdict in ablation.judge(CONDITIONAL, results, floor=0.01)}
        assert by_signal["layer"].delta == pytest.approx(-0.2)
        assert by_signal["layer"].label == "earns its place"

    def test_a_block_lists_its_reference_first(self):
        block = Block("t", FULL, {"x": NO_HRR})
        assert block.configurations == (FULL, NO_HRR)

    def test_report_names_the_inputs_and_every_configuration(self):
        results = _results(full=0.5)
        noise_runs = {"full": [results["full"]] * 2, "no-hrr": [results["no-hrr"]] * 2}
        report = ablation.render_report(
            results, noise_runs, run_date=date(2026, 9, 6), digest="abc123", provider_name="test/x"
        )
        assert "Run date: 2026-09-06" in report
        assert "`abc123`" in report
        assert "`test/x`" in report
        assert all(f"| {configuration.name} |" in report for configuration in CONFIGURATIONS)
        assert all(f"| {signal} |" in report for signal in PRIMARY.ablated_by_signal)
        assert all(f"## {block.title}" in report for block in BLOCKS)


def _results(**mrr_by_name: float) -> dict[str, dict[str, Metrics]]:
    def metrics(mrr: float) -> dict[str, Metrics]:
        return {name: Metrics(mrr, 1.0, 5) for name in (OVERALL, *SIGNALS)}

    return {c.name: metrics(mrr_by_name.get(c.name, mrr_by_name["full"])) for c in CONFIGURATIONS}


@pytest.mark.skipif(os.environ.get(ABLATION_FLAG) != "1", reason=f"set {ABLATION_FLAG}=1 to run the ablation")
class TestAblationRun:
    async def test_measure_every_configuration_and_write_the_report(self, pg, provider, cache, monkeypatch):
        seeding.wire_server(monkeypatch, pg, provider, cache)
        await run_migrations(pg)
        corpus, cases = harness.load_corpus(), harness.load_cases()

        await seeding.clean(pg)
        keys_by_id = await seeding.seed_corpus(pg, corpus)
        results = {}
        for configuration in CONFIGURATIONS:
            with ablation.applied(configuration):
                results[configuration.name] = await _measure(pg, provider, cases, keys_by_id)

        references = [block.reference for block in BLOCKS]
        noise_runs = {reference.name: [] for reference in references}
        for seed in range(NOISE_FLOOR_RUNS):
            await seeding.clean(pg)
            keys_by_id = await seeding.seed_corpus(pg, ablation.shuffled(corpus, seed))
            for reference in references:
                with ablation.applied(reference):
                    noise_runs[reference.name].append(await _measure(pg, provider, cases, keys_by_id))
        await seeding.clean(pg)

        report = ablation.render_report(
            results, noise_runs, run_date=date.today(), digest=harness.corpus_digest(), provider_name=provider.name
        )
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report)
        print(f"report written to {REPORT_PATH}\n{report}")


async def _measure(pg, provider, cases, keys_by_id) -> dict[str, Metrics]:
    return harness.summarize(await seeding.run_cases(pg, provider, cases, keys_by_id))
