"""Ranking-signal ablation — does each signal earn its complexity?

The ranker fuses six things: a vector leg, a keyword leg, an HRR leg, and
three quality multipliers (decay, trust, layer weight). Each was added with an
argument and none with a measurement. This module switches them off one at a
time and reruns the golden set, so the question "what does this signal buy"
gets a number instead of an opinion. It is measurement only: nothing in
``src/`` changes, and the report it writes is the deliverable.

**How a signal is switched off.** Every configuration is a variant of the
production ``RRF_QUERY_TEMPLATE`` plus, for HRR, a stand-in for
``_hrr_leg`` that contributes zero to every candidate. The variants come from exact
replacement of fragments quoted verbatim from the template — the quality-weight
product, and each leg's predicate — and :func:`variant` refuses to run when a
fragment is not found exactly where expected, so a change to the production
statement fails the ablation loudly instead of measuring ``full`` under another
name. The seeded corpus is identical across configurations; only the ranking
function changes, which is what makes the deltas attributable.

**Two blocks.** The primary block removes one signal from ``full``, as issue
#94 asked. The conditional block removes the HRR leg first and then one more
signal. It was added when the leg was a boost whose floor (+0.075 at zero
similarity, against a maximum RRF sum of ~0.033) flattened relevance so far
that under ``full`` every multiplicative weight decided more than it should,
and removing one looked like an improvement; #103 replaced the boost, and the
block stays because it still answers a live question — which signals stand on
their own when the HRR leg contributes nothing, as it does for memories stored
without a vector.

**Noise floor.** Insertion order reaches the ranking only through
``created_at``, which breaks ties at every ``ORDER BY … LIMIT``. Reseeding the
same corpus in shuffled orders and rerunning a block's reference configuration
therefore measures how much of a metric ties alone can move; the floor is the
spread (max − min) of overall MRR@10 across those runs. Each block has its own
floor, since ``full`` and ``no-hrr`` do not tie in the same places.

**Decision rule.** For each signal, ``Δ = MRR@10(without) − MRR@10(reference)``.
``Δ < −floor``: the signal earns its place. ``|Δ| ≤ floor``: below the noise
floor — a candidate for removal or redesign, filed as a follow-up with the
numbers. ``Δ > floor``: the signal hurts retrieval as implemented — also a
follow-up, and the more urgent one. The rule is applied to overall MRR@10; the
per-signal slices are reported so a follow-up can name the cases that moved.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

from synapto.search import hybrid
from synapto.sql import search as sql
from tests.eval.harness import OVERALL, SIGNALS, Memory, Metrics

NOISE_FLOOR_RUNS = 5


class AblationError(RuntimeError):
    """The production statement no longer contains a fragment the ablation switches off."""


@dataclass(frozen=True)
class Configuration:
    """Which signals stay on. ``full`` is every field at its default."""

    name: str
    hrr: bool = True
    decay: bool = True
    trust: bool = True
    layer: bool = True
    vector_leg: bool = True
    keyword_leg: bool = True


@dataclass(frozen=True)
class Block:
    """A reference configuration and the configurations that each remove one more signal from it."""

    title: str
    reference: Configuration
    ablated_by_signal: Mapping[str, Configuration]

    @property
    def configurations(self) -> tuple[Configuration, ...]:
        return (self.reference, *self.ablated_by_signal.values())


FULL = Configuration("full")
NO_HRR = Configuration("no-hrr", hrr=False)
RRF_ONLY = Configuration("rrf-only", hrr=False, decay=False, trust=False, layer=False)

PRIMARY = Block(
    title="One signal removed from `full`",
    reference=FULL,
    ablated_by_signal={
        "hrr": NO_HRR,
        "decay": Configuration("no-decay", decay=False),
        "trust": Configuration("no-trust", trust=False),
        "layer": Configuration("no-layer", layer=False),
        "vector leg": Configuration("keyword-only", vector_leg=False),
        "keyword leg": Configuration("vector-only", keyword_leg=False),
    },
)
CONDITIONAL = Block(
    title="One more signal removed from `no-hrr`",
    reference=NO_HRR,
    ablated_by_signal={
        "decay": Configuration("no-hrr-no-decay", hrr=False, decay=False),
        "trust": Configuration("no-hrr-no-trust", hrr=False, trust=False),
        "layer": Configuration("no-hrr-no-layer", hrr=False, layer=False),
        "vector leg": Configuration("no-hrr-keyword-only", hrr=False, vector_leg=False),
        "keyword leg": Configuration("no-hrr-vector-only", hrr=False, keyword_leg=False),
    },
)
BLOCKS = (PRIMARY, CONDITIONAL)
CONFIGURATIONS: tuple[Configuration, ...] = (*PRIMARY.configurations, RRF_ONLY, *CONDITIONAL.configurations[1:])

LAYER_WEIGHT = """CASE m.depth_layer
        WHEN 'core' THEN 1.5
        WHEN 'stable' THEN 1.2
        WHEN 'working' THEN 1.0
        WHEN 'ephemeral' THEN 0.5
        ELSE 1.0
    END"""
DECAY_WEIGHT = "m.decay_score"
TRUST_WEIGHT = "m.trust_score"
QUALITY_WEIGHT = f"{DECAY_WEIGHT} * {TRUST_WEIGHT} * {LAYER_WEIGHT}"
NO_WEIGHT = "1.0"
VECTOR_LEG_TAIL = "{{filters}}\n    ORDER BY embedding"
KEYWORD_LEG_PREDICATE = "AND tsv @@ plainto_tsquery('english', %(query)s)"
DISABLED = "AND false"


def weight_expression(configuration: Configuration) -> str:
    """The product of the quality factors still switched on, or ``1.0`` when none is."""
    factors = [
        factor
        for enabled, factor in (
            (configuration.decay, DECAY_WEIGHT),
            (configuration.trust, TRUST_WEIGHT),
            (configuration.layer, LAYER_WEIGHT),
        )
        if enabled
    ]
    return " * ".join(factors) or NO_WEIGHT


def variant(configuration: Configuration, template: str = sql.RRF_QUERY_TEMPLATE) -> str:
    """The production statement with the configuration's signals switched off.

    Raises:
        AblationError: a fragment is missing or duplicated, meaning the
            production statement drifted from the one these fragments describe.
    """
    statement = _swap(template, QUALITY_WEIGHT, weight_expression(configuration), expected=2)
    if not configuration.vector_leg:
        statement = _swap(statement, VECTOR_LEG_TAIL, f"{DISABLED}\n      {VECTOR_LEG_TAIL}", expected=1)
    if not configuration.keyword_leg:
        statement = _swap(statement, KEYWORD_LEG_PREDICATE, DISABLED, expected=1)
    return statement


def _swap(statement: str, fragment: str, replacement: str, *, expected: int) -> str:
    found = statement.count(fragment)
    if found != expected:
        raise AblationError(
            f"expected {expected} occurrence(s) of {fragment.splitlines()[0]!r} in RRF_QUERY_TEMPLATE, found {found}; "
            "the production statement changed — update the fragments in tests/eval/ablation.py"
        )
    return statement.replace(fragment, replacement)


def _no_hrr_leg(rows: Sequence[Mapping[str, object]], query: str, rrf_k: int) -> list[float]:
    return [0.0] * len(rows)


@contextmanager
def applied(configuration: Configuration) -> Iterator[None]:
    """Run ``hybrid_search`` under the configuration for the duration of the block."""
    hrr_leg = hybrid._hrr_leg if configuration.hrr else _no_hrr_leg
    with (
        patch.object(sql, "RRF_QUERY_TEMPLATE", variant(configuration)),
        patch.object(hybrid, "_hrr_leg", hrr_leg),
    ):
        yield


def shuffled(corpus: Sequence[Memory], seed: int) -> list[Memory]:
    """The corpus in an insertion order decided by ``seed`` alone, so a run can be repeated."""
    return random.Random(seed).sample(list(corpus), len(corpus))


def noise_floor(runs: Sequence[Mapping[str, Metrics]]) -> float:
    """Spread of overall MRR@10 across repeated runs of one configuration."""
    scores = [run[OVERALL].mrr_at_10 for run in runs]
    return max(scores) - min(scores)


@dataclass(frozen=True)
class Verdict:
    signal: str
    configuration: str
    delta: float
    floor: float

    @property
    def label(self) -> str:
        if abs(self.delta) <= self.floor:
            return "below the noise floor"
        return "earns its place" if self.delta < 0 else "hurts retrieval"

    @property
    def needs_follow_up(self) -> bool:
        return self.label != "earns its place"


def judge(block: Block, results: Mapping[str, Mapping[str, Metrics]], floor: float) -> list[Verdict]:
    """One verdict per signal in the block, against the block's reference."""
    reference = results[block.reference.name][OVERALL].mrr_at_10
    return [
        Verdict(signal, configuration.name, results[configuration.name][OVERALL].mrr_at_10 - reference, floor)
        for signal, configuration in block.ablated_by_signal.items()
    ]


def render_report(
    results: Mapping[str, Mapping[str, Metrics]],
    noise_runs: Mapping[str, Sequence[Mapping[str, Metrics]]],
    *,
    run_date: date,
    digest: str,
    provider_name: str,
) -> str:
    """The committed deliverable: inputs, results and verdicts per block, noise floors, as Markdown.

    ``noise_runs`` maps each block's reference configuration name to its
    shuffled-order runs; ``results`` holds every configuration in
    :data:`CONFIGURATIONS`, measured on the canonical insertion order.
    """
    floors = {name: noise_floor(runs) for name, runs in noise_runs.items()}
    lines = [
        "# Ranking-signal ablation",
        "",
        f"- Run date: {run_date.isoformat()}",
        f"- Corpus digest: `{digest}`",
        f"- Embedding provider: `{provider_name}`",
        f"- Cases: {results[FULL.name][OVERALL].cases}",
        *[
            f"- Noise floor for `{name}` (overall MRR@10 spread over {len(noise_runs[name])} shuffled runs): "
            f"{floor:.4f}"
            for name, floor in floors.items()
        ],
        "",
        "Generated by `tests/eval/test_ablation.py`; see `tests/eval/ablation.py` for the method and "
        "issue #94 for the question. Regenerate with `SYNAPTO_EVAL_ABLATION=1 uv run pytest tests/eval`.",
        "",
        "Δ is overall MRR@10 against the block's reference. Per-signal columns are MRR@10 on that slice "
        "of the golden set. Rule: Δ < −floor earns its place; |Δ| ≤ floor is below the noise floor; "
        "Δ > floor hurts retrieval. Anything but the first gets a follow-up issue.",
    ]
    for block in BLOCKS:
        floor = floors[block.reference.name]
        lines += ["", f"## {block.title}", "", *_results_table(block.configurations, results, block.reference)]
        lines += ["", f"Verdicts against `{block.reference.name}`, floor {floor:.4f}:", ""]
        lines += _verdict_table(judge(block, results, floor))
    lines += ["", "## Every quality signal removed", "", *_results_table((FULL, RRF_ONLY), results, FULL)]
    lines += ["", "## Noise floor", "", *_noise_table(noise_runs)]
    return "\n".join(lines) + "\n"


def _results_table(
    configurations: Sequence[Configuration],
    results: Mapping[str, Mapping[str, Metrics]],
    reference: Configuration,
) -> list[str]:
    baseline = results[reference.name][OVERALL].mrr_at_10
    rows = [
        "| configuration | MRR@10 | Δ | Recall@5 | " + " | ".join(SIGNALS) + " |",
        "|---|---:|---:|---:|" + "---:|" * len(SIGNALS),
    ]
    for configuration in configurations:
        metrics = results[configuration.name]
        overall = metrics[OVERALL]
        per_signal = " | ".join(f"{metrics[signal].mrr_at_10:.4f}" for signal in SIGNALS)
        rows.append(
            f"| {configuration.name} | {overall.mrr_at_10:.4f} | {overall.mrr_at_10 - baseline:+.4f} | "
            f"{overall.recall_at_5:.4f} | {per_signal} |"
        )
    return rows


def _verdict_table(verdicts: Sequence[Verdict]) -> list[str]:
    rows = ["| signal | removed in | Δ MRR@10 | verdict |", "|---|---|---:|---|"]
    rows += [f"| {v.signal} | {v.configuration} | {v.delta:+.4f} | {v.label} |" for v in verdicts]
    return rows


def _noise_table(noise_runs: Mapping[str, Sequence[Mapping[str, Metrics]]]) -> list[str]:
    names = list(noise_runs)
    rows = [
        "Each reference configuration, reseeded in a different insertion order per seed. "
        "Only `created_at` tie-breaking differs between seeds.",
        "",
        "| seed | " + " | ".join(f"{name} MRR@10" for name in names) + " |",
        "|---:|" + "---:|" * len(names),
    ]
    for seed in range(len(noise_runs[names[0]])):
        cells = " | ".join(f"{noise_runs[name][seed][OVERALL].mrr_at_10:.4f}" for name in names)
        rows.append(f"| {seed} | {cells} |")
    return rows
