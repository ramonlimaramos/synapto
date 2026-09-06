"""The layer-spread sweep runner and the contracts behind its table.

The runner is opt-in (``SYNAPTO_EVAL_LAYER_SWEEP=1``): it seeds the corpus,
measures every candidate spread under ``full`` and ``no-hrr``, reseeds in
shuffled orders for the noise floor of the production spread, and writes
``docs/eval/layer_sweep.md``. Everything else runs always: the current spread
reproduces the production statement byte for byte, every candidate is
executable SQL, the spread really reaches the reported score, and the choice
rule is pinned on synthetic numbers.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from synapto.db.migrations import run_migrations
from synapto.search.hybrid import DEPTH_BOOST, hybrid_search
from synapto.sql.search import RRF_QUERY_TEMPLATE
from tests.eval import ablation, harness, layer_sweep, seeding
from tests.eval.ablation import FULL, NO_HRR, NOISE_FLOOR_RUNS, AblationError
from tests.eval.conftest import TWINS_QUERY, TWINS_TENANT
from tests.eval.harness import OVERALL, SIGNALS, Metrics
from tests.eval.layer_sweep import BEFORE_104, CANDIDATES, COLUMNS, CURRENT, FLAT, Spread

SWEEP_FLAG = "SYNAPTO_EVAL_LAYER_SWEEP"
REPORT_PATH = harness.EVAL_DIR.parent.parent / "docs" / "eval" / "layer_sweep.md"


class TestSpread:
    def test_the_current_spread_is_the_production_one(self):
        assert CURRENT == Spread(**DEPTH_BOOST)
        assert layer_sweep.variant(CURRENT) == RRF_QUERY_TEMPLATE

    def test_names_read_core_stable_working_ephemeral(self):
        assert CURRENT.name == "1.3/1.25/1.0/0.7"
        assert BEFORE_104.name == "1.5/1.2/1.0/0.5"
        assert FLAT.name == "1.0/1.0/1.0/1.0"

    def test_candidates_are_unique_and_keep_the_current_the_previous_and_the_flat_spread(self):
        names = [spread.name for spread in CANDIDATES]
        assert len(names) == len(set(names))
        assert {CURRENT, BEFORE_104, FLAT} <= set(CANDIDATES)

    def test_width_is_symmetric_on_a_log_scale(self):
        assert Spread(core=2.0, stable=1.0, ephemeral=1.0).width == pytest.approx(
            Spread(core=1.0, stable=1.0, ephemeral=0.5).width
        )
        assert FLAT.width == 0.0

    def test_candidates_narrow_monotonically_toward_flat(self):
        widths = [spread.width for spread in CANDIDATES]
        assert widths == sorted(widths, reverse=True)


class TestVariant:
    @pytest.mark.parametrize("spread", CANDIDATES, ids=lambda s: s.name)
    def test_every_candidate_rewrites_both_case_blocks_and_still_formats(self, spread):
        statement = layer_sweep.variant(spread)
        assert statement.count(f"WHEN 'core' THEN {spread.core}") == 2
        assert statement.count(f"WHEN 'ephemeral' THEN {spread.ephemeral}") == 2
        statement.format(dim=384).format(filters="")

    def test_the_no_hrr_column_keeps_the_ablation_switch(self):
        assert layer_sweep.variant(FLAT, NO_HRR) == layer_sweep.variant(FLAT, FULL)

    def test_a_drifted_template_is_refused(self):
        with (
            pytest.raises(AblationError, match="production statement changed"),
            pytest.MonkeyPatch.context() as patched,
        ):
            patched.setattr(ablation, "LAYER_WEIGHT", "CASE nothing END")
            layer_sweep.variant(FLAT)


async def _scores(pg, provider, spread: Spread, configuration=FULL) -> dict:
    with layer_sweep.applied(spread, configuration):
        results = await hybrid_search(pg, provider, TWINS_QUERY, tenant=TWINS_TENANT, limit=10)
    return {result.id: result.rrf_score for result in results}


class TestTheSpreadReachesTheScore:
    async def test_the_twins_ratio_is_the_spread_ratio(self, pg, provider, twins):
        core, ephemeral = twins
        for spread in CANDIDATES:
            scores = await _scores(pg, provider, spread)
            assert scores[core] == pytest.approx(scores[ephemeral] * spread.core / spread.ephemeral), spread.name

    async def test_the_no_hrr_column_drops_the_hrr_leg(self, pg, provider, twins):
        core, _ = twins
        assert (await _scores(pg, provider, CURRENT, NO_HRR))[core] < (await _scores(pg, provider, CURRENT, FULL))[core]

    async def test_the_patch_does_not_outlive_the_block(self, pg, provider, twins):
        core, ephemeral = twins
        await _scores(pg, provider, FLAT)
        after = await _scores(pg, provider, CURRENT)
        assert after[core] == pytest.approx(after[ephemeral] * CURRENT.core / CURRENT.ephemeral)


def _metrics(overall: float, layer: float = 1.0) -> dict[str, Metrics]:
    return {name: Metrics(layer if name == "layer" else overall, 1.0, 6) for name in (OVERALL, *SIGNALS)}


class TestChoice:
    def test_a_spread_that_misses_a_layer_case_is_not_admissible(self):
        assert layer_sweep.admissible(_metrics(0.99, layer=0.9167)) is False
        assert layer_sweep.admissible(_metrics(0.5, layer=1.0)) is True

    def test_the_best_admissible_overall_wins(self):
        results = {spread.name: _metrics(0.80) for spread in CANDIDATES}
        results[CANDIDATES[2].name] = _metrics(0.90)
        results[CANDIDATES[1].name] = _metrics(0.95, layer=0.9)
        assert layer_sweep.choose(results, floor=0.003) == CANDIDATES[2]

    def test_a_tie_within_the_floor_goes_to_the_narrower_spread(self):
        results = {spread.name: _metrics(0.80) for spread in CANDIDATES}
        results[CANDIDATES[1].name] = _metrics(0.902)
        results[CANDIDATES[4].name] = _metrics(0.900)
        assert layer_sweep.choose(results, floor=0.003) == CANDIDATES[4]
        assert layer_sweep.choose(results, floor=0.001) == CANDIDATES[1]

    def test_no_admissible_spread_is_a_finding_not_an_error(self):
        results = {spread.name: _metrics(0.9, layer=0.5) for spread in CANDIDATES}
        assert layer_sweep.choose(results, floor=0.003) is None

    def test_report_names_the_inputs_every_spread_and_the_choice(self):
        by_spread = {spread.name: _metrics(0.9) for spread in CANDIDATES}
        results = {configuration.name: by_spread for configuration in COLUMNS}
        report = layer_sweep.render_report(
            results, 0.003, run_date=date(2026, 9, 6), digest="abc123", provider_name="test/x"
        )
        assert "Run date: 2026-09-06" in report
        assert "`abc123`" in report
        assert "`test/x`" in report
        assert all(f"| {spread.name} |" in report for spread in CANDIDATES)
        assert all(f"## Under `{configuration.name}`" in report for configuration in COLUMNS)
        assert f"`{FLAT.name}` — overall MRR@10 0.9000" in report, "all tied: the narrowest wins"


@pytest.mark.skipif(os.environ.get(SWEEP_FLAG) != "1", reason=f"set {SWEEP_FLAG}=1 to run the sweep")
class TestSweepRun:
    async def test_measure_every_spread_and_write_the_report(self, pg, provider, cache, monkeypatch):
        seeding.wire_server(monkeypatch, pg, provider, cache)
        await run_migrations(pg)
        corpus, cases = harness.load_corpus(), harness.load_cases()

        await seeding.clean(pg)
        keys_by_id = await seeding.seed_corpus(pg, corpus)
        results: dict[str, dict[str, dict[str, Metrics]]] = {configuration.name: {} for configuration in COLUMNS}
        for configuration in COLUMNS:
            for spread in CANDIDATES:
                with layer_sweep.applied(spread, configuration):
                    results[configuration.name][spread.name] = await _measure(pg, provider, cases, keys_by_id)

        noise_runs = []
        for seed in range(NOISE_FLOOR_RUNS):
            await seeding.clean(pg)
            keys_by_id = await seeding.seed_corpus(pg, ablation.shuffled(corpus, seed))
            with layer_sweep.applied(CURRENT, FULL):
                noise_runs.append(await _measure(pg, provider, cases, keys_by_id))
        await seeding.clean(pg)

        report = layer_sweep.render_report(
            results,
            ablation.noise_floor(noise_runs),
            run_date=date.today(),
            digest=harness.corpus_digest(),
            provider_name=provider.name,
        )
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report)
        print(f"report written to {REPORT_PATH}\n{report}")


async def _measure(pg, provider, cases, keys_by_id) -> dict[str, Metrics]:
    return harness.summarize(await seeding.run_cases(pg, provider, cases, keys_by_id))
