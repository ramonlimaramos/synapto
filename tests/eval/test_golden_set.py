"""The retrieval gate and the invariants of the golden set itself.

The gate seeds the corpus in file order through :mod:`tests.eval.seeding`, runs
every case through ``hybrid_search`` and compares the per-signal metrics with
``baseline.json``.

``SYNAPTO_EVAL_WRITE_BASELINE=1`` turns the comparison into a write: the run
records what it measured and passes. That is the only way the baseline changes,
so a diff to ``baseline.json`` in a pull request is always a deliberate claim
about retrieval quality, reviewed next to the code that moved it.

The invariant tests need no database: they keep the corpus and the cases
consistent (every expected key exists, every signal has enough cases) and pin
the metric arithmetic so a regression in the harness cannot masquerade as one
in the ranker.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest

from synapto.db.migrations import run_migrations
from tests.eval import harness, seeding
from tests.eval.harness import (
    BASELINE_PATH,
    MIN_CASES_PER_SIGNAL,
    SIGNALS,
    Case,
    GoldenSetError,
    Memory,
    Metrics,
    Outcome,
)

WRITE_BASELINE_FLAG = "SYNAPTO_EVAL_WRITE_BASELINE"


@pytest.fixture
async def corpus(pg, provider, cache, monkeypatch) -> dict[UUID, str]:
    """Seed the corpus under ``acme/eval`` in file order and map each stored id back to its key.

    Migrations run first because ``tests/eval`` sorts before ``tests/unit`` and
    may be the first thing to touch a fresh CI database; ``run_migrations`` is
    idempotent, so on a migrated database it is a no-op.
    """
    seeding.wire_server(monkeypatch, pg, provider, cache)
    await run_migrations(pg)
    await seeding.clean(pg)
    yield await seeding.seed_corpus(pg, harness.load_corpus())
    await seeding.clean(pg)


class TestRetrievalGate:
    async def test_metrics_match_the_committed_baseline(self, pg, provider, corpus):
        outcomes = await seeding.run_cases(pg, provider, harness.load_cases(), corpus)
        observed = harness.summarize(outcomes)
        report = f"{harness.render_table(observed)}\n\nmisses:\n{harness.render_misses(outcomes)}"

        if os.environ.get(WRITE_BASELINE_FLAG) == "1":
            harness.write_baseline(harness.render_baseline(observed, provider.name, harness.corpus_digest()))
            print(f"baseline written to {BASELINE_PATH}\n{report}")
            return

        baseline = harness.load_baseline()
        assert baseline["provider"] == provider.name, "baseline was recorded with a different embedding provider"
        assert baseline["corpus_digest"] == harness.corpus_digest(), (
            f"corpus or cases changed since the baseline; re-run with {WRITE_BASELINE_FLAG}=1\n{report}"
        )
        deviations = harness.compare(observed, baseline)
        assert deviations == [], "\n".join(deviations) + "\n\n" + report


class TestGoldenSetInvariants:
    def test_every_case_points_at_a_corpus_key_and_every_signal_is_covered(self):
        harness.check_consistency(harness.load_corpus(), harness.load_cases())

    def test_signals_meet_the_minimum(self):
        counts = {signal: 0 for signal in SIGNALS}
        for case in harness.load_cases():
            counts[case.signal] += 1
        assert all(count >= MIN_CASES_PER_SIGNAL for count in counts.values()), counts

    def test_corpus_is_synthetic(self):
        for memory in harness.load_corpus():
            repo_scopes = [scope for scope in memory.scopes if scope.startswith("repo:")]
            assert all(scope.startswith("repo:acme/") for scope in repo_scopes), memory.key
            paths = [token.strip("`.,;:") for token in memory.content.split() if "/" in token]
            assert all(token.startswith(("acme/", "config/")) for token in paths), memory.key

    def test_baseline_names_the_current_inputs(self):
        baseline = harness.load_baseline()
        assert baseline["corpus_digest"] == harness.corpus_digest()
        assert baseline["metrics"]["overall"]["cases"] == len(harness.load_cases())

    def test_missing_expected_key_is_rejected(self):
        corpus = [Memory(key="a", content="x")]
        cases = [
            Case(signal=signal, query="q", expected="a") for signal in SIGNALS for _ in range(MIN_CASES_PER_SIGNAL)
        ]
        harness.check_consistency(corpus, cases)
        with pytest.raises(GoldenSetError, match="absent from the corpus"):
            harness.check_consistency(corpus, [*cases, Case(signal="layer", query="q", expected="ghost")])

    def test_thin_signal_is_rejected(self):
        corpus = [Memory(key="a", content="x")]
        cases = [Case(signal="layer", query="q", expected="a")]
        with pytest.raises(GoldenSetError, match="fewer than"):
            harness.check_consistency(corpus, cases)

    def test_duplicate_corpus_keys_are_rejected(self, tmp_path):
        path = tmp_path / "corpus.toml"
        path.write_text('[[memory]]\nkey = "a"\ncontent = "x"\n[[memory]]\nkey = "a"\ncontent = "y"\n')
        with pytest.raises(GoldenSetError, match="duplicate corpus keys"):
            harness.load_corpus(path)

    def test_unknown_signal_file_is_rejected(self, tmp_path):
        (tmp_path / "vibes.toml").write_text('[[case]]\nquery = "q"\nexpected = "a"\n')
        with pytest.raises(GoldenSetError, match="unknown signal"):
            harness.load_cases(tmp_path)


class TestMetricArithmetic:
    @pytest.mark.parametrize(
        ("ranked", "expected_rr"),
        [(("a", "b"), 1.0), (("b", "a"), 0.5), (("b", "c", "a"), 1 / 3), (("b",), 0.0), ((), 0.0)],
    )
    def test_reciprocal_rank(self, ranked, expected_rr):
        assert harness.reciprocal_rank(ranked, "a", cutoff=10) == pytest.approx(expected_rr)

    def test_reciprocal_rank_is_zero_past_the_cutoff(self):
        ranked = tuple(f"d{i}" for i in range(10)) + ("a",)
        assert harness.reciprocal_rank(ranked, "a", cutoff=10) == 0.0

    def test_recall_uses_the_top_five(self):
        case = Case(signal="general", query="q", expected="a")
        assert Outcome(case, ("x", "y", "z", "w", "a")).recalled
        assert not Outcome(case, ("x", "y", "z", "w", "v", "a")).recalled

    def test_summarize_reports_overall_and_per_signal(self):
        outcomes = [
            Outcome(Case("layer", "q", "a"), ("a",)),
            Outcome(Case("layer", "q", "a"), ("b", "a")),
            Outcome(Case("trust", "q", "a"), ("b", "c", "d", "e", "f", "a")),
        ]
        summary = harness.summarize(outcomes)
        assert summary["layer"] == Metrics(mrr_at_10=0.75, recall_at_5=1.0, cases=2)
        assert summary["trust"] == Metrics(mrr_at_10=pytest.approx(1 / 6), recall_at_5=0.0, cases=1)
        assert summary["overall"].cases == 3
        assert "decay" not in summary

    def test_compare_flags_drops_and_rises_beyond_tolerance(self):
        baseline = {"metrics": {"overall": {"mrr_at_10": 0.9, "recall_at_5": 0.9, "cases": 1}}}
        steady = {"overall": Metrics(0.9 - harness.TOLERANCE, 0.9 + harness.TOLERANCE, 1)}
        assert harness.compare(steady, baseline) == []
        moved = {"overall": Metrics(0.8, 1.0, 1)}
        deviations = harness.compare(moved, baseline)
        assert any("regressed" in line for line in deviations)
        assert any("re-baseline" in line for line in deviations)
