"""Layer-weight spread sweep — how far should ``depth_layer`` move the order?

The ablation (:mod:`tests.eval.ablation`) answers whether the layer weight
earns its place; it does not say what size it should be. Issue #104 measured
the cost of the original ``1.5 / 1.2 / 1.0 / 0.5``: the six ``layer`` cases
need *some* weight, because at equal relevance ``core`` must beat ``working``,
and the eighteen ``general`` cases paid for a 3× spread that let a weaker
match in a higher layer outrank the memory that answered the query. This
module reruns the golden set under candidate spreads and writes the
per-spread table, so the value in ``src/synapto/sql/search.py`` is chosen by
a number rather than by feel. It is measurement only: :data:`CURRENT` must
equal what production ships, and a test pins it.

**What the first sweep found (2026-09-06).** Narrowing alone does not work
under ``full``: the binding ratio is ``stable / working``, not the overall
width. A lower-layer memory that restates the query verbatim earns a full HRR
leg (evidence 1.0), so the higher layer needs about 1.25× over ``working`` to
keep the authoritative version first; every spread with ``stable`` below that
misses ``layer-04``. Without the HRR leg the picture inverts — the narrowest
spread wins — which is why the ``no-hrr`` column stays in the table.

**How a spread is applied.** Each candidate is the production statement with
the ``CASE`` arms rewritten — the same exact-fragment replacement the ablation
uses, so a drift in the template fails the sweep loudly — and, for the
``no-hrr`` column, the ablation's stand-in HRR leg. The corpus is identical
across spreads; only the arms change, which is what makes the columns
comparable.

**Decision rule.** A spread is *admissible* when the ``layer`` slice is 1.0
under ``full`` — the six cases that exist to justify the weight all rank their
target first. Among admissible spreads the highest overall MRR@10 wins; two
spreads within the noise floor of each other are a tie, and the narrower one
wins the tie because a smaller multiplier disturbs fewer orders it was not
asked to decide. The flat spread (every arm 1.0) is always in the table as the
lower bound: it is the ablation's ``no-layer`` under another name.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

from synapto.sql import search as sql
from tests.eval import ablation
from tests.eval.ablation import FULL, LAYER_WEIGHT, NO_HRR, Configuration
from tests.eval.harness import OVERALL, SIGNALS, Metrics

LAYERS = ("core", "stable", "working", "ephemeral")
PERFECT = 1.0


@dataclass(frozen=True)
class Spread:
    """One weight per depth layer; ``working`` is the unit the others are measured against."""

    core: float
    stable: float
    ephemeral: float
    working: float = 1.0

    @property
    def name(self) -> str:
        return "/".join(str(getattr(self, layer)) for layer in LAYERS)

    @property
    def width(self) -> float:
        """How far the spread reaches from 1.0 in either direction, on a log scale, so ×1.5 and ×0.67 weigh the same."""
        return max(abs(math.log(getattr(self, layer))) for layer in LAYERS)

    def case_expression(self) -> str:
        """The ``CASE`` fragment in the template's exact layout, so the current spread reproduces it verbatim."""
        arms = "\n".join(f"        WHEN '{layer}' THEN {getattr(self, layer)}" for layer in LAYERS)
        return f"CASE m.depth_layer\n{arms}\n        ELSE 1.0\n    END"


CURRENT = Spread(core=1.3, stable=1.25, ephemeral=0.7)
BEFORE_104 = Spread(core=1.5, stable=1.2, ephemeral=0.5)
FLAT = Spread(core=1.0, stable=1.0, ephemeral=1.0)
CANDIDATES: tuple[Spread, ...] = (
    BEFORE_104,
    Spread(core=1.5, stable=1.3, ephemeral=0.5),
    Spread(core=1.4, stable=1.3, ephemeral=0.7),
    CURRENT,
    Spread(core=1.3, stable=1.15, ephemeral=0.7),
    Spread(core=1.2, stable=1.1, ephemeral=0.8),
    Spread(core=1.15, stable=1.05, ephemeral=0.85),
    Spread(core=1.1, stable=1.05, ephemeral=0.9),
    Spread(core=1.05, stable=1.02, ephemeral=0.95),
    FLAT,
)
COLUMNS: tuple[Configuration, ...] = (FULL, NO_HRR)


def variant(spread: Spread, configuration: Configuration = FULL) -> str:
    """The configuration's statement with the layer arms replaced by ``spread``.

    Raises:
        AblationError: the arms are not where the ablation's fragment says,
            meaning the production statement drifted.
    """
    return ablation._swap(ablation.variant(configuration), LAYER_WEIGHT, spread.case_expression(), expected=2)


@contextmanager
def applied(spread: Spread, configuration: Configuration = FULL) -> Iterator[None]:
    """Run ``hybrid_search`` under the spread, and the configuration's other switches, for the block."""
    with ablation.applied(configuration), patch.object(sql, "RRF_QUERY_TEMPLATE", variant(spread, configuration)):
        yield


def admissible(metrics: Mapping[str, Metrics]) -> bool:
    """Every ``layer`` case ranks its target first."""
    return metrics["layer"].mrr_at_10 >= PERFECT


def choose(results: Mapping[str, Mapping[str, Metrics]], floor: float) -> Spread | None:
    """The admissible spread with the best overall MRR@10 under ``full``; ties within the floor go to the narrower.

    ``results`` maps a spread name to its ``full`` metrics. Returns ``None``
    when no candidate is admissible, which is itself a finding: the cases
    cannot be satisfied by a multiplier alone.
    """
    admitted = [spread for spread in CANDIDATES if admissible(results[spread.name])]
    if not admitted:
        return None
    best = max(results[spread.name][OVERALL].mrr_at_10 for spread in admitted)
    within_floor = [spread for spread in admitted if best - results[spread.name][OVERALL].mrr_at_10 <= floor]
    return min(within_floor, key=lambda spread: spread.width)


def render_report(
    results: Mapping[str, Mapping[str, Mapping[str, Metrics]]],
    floor: float,
    *,
    run_date: date,
    digest: str,
    provider_name: str,
) -> str:
    """The committed deliverable: one table per column configuration, then the choice, as Markdown.

    ``results`` maps a configuration name (``full``, ``no-hrr``) to a mapping of
    spread name to metrics. ``floor`` is the noise floor measured for
    :data:`CURRENT` under ``full``, the reference the choice is judged against.
    """
    chosen = choose(results[FULL.name], floor)
    lines = [
        "# Layer-weight spread sweep",
        "",
        f"- Run date: {run_date.isoformat()}",
        f"- Corpus digest: `{digest}`",
        f"- Embedding provider: `{provider_name}`",
        f"- Cases: {results[FULL.name][CURRENT.name][OVERALL].cases}",
        f"- Noise floor for `{CURRENT.name}` under `full` (overall MRR@10 spread over shuffled runs): {floor:.4f}",
        "",
        "Generated by `tests/eval/test_layer_sweep.py`; see `tests/eval/layer_sweep.py` for the method and "
        "issue #104 for the question. Regenerate with `SYNAPTO_EVAL_LAYER_SWEEP=1 uv run pytest tests/eval`.",
        "",
        "Spreads read `core/stable/working/ephemeral`. Δ is overall MRR@10 against the spread in production "
        f"(`{CURRENT.name}`). A spread is admissible when the `layer` slice is 1.0 under `full`; among admissible "
        "spreads the best overall MRR@10 wins, and a tie within the floor goes to the narrower spread.",
    ]
    for configuration in COLUMNS:
        lines += ["", f"## Under `{configuration.name}`", "", *_results_table(results[configuration.name])]
    lines += ["", "## Choice", ""]
    if chosen is None:
        lines.append(
            "No candidate keeps the `layer` slice at 1.0 under `full`; a multiplier alone cannot satisfy the cases."
        )
    else:
        lines.append(
            f"`{chosen.name}` — overall MRR@10 {results[FULL.name][chosen.name][OVERALL].mrr_at_10:.4f} under `full`, "
            f"`layer` slice {results[FULL.name][chosen.name]['layer'].mrr_at_10:.4f}."
        )
    return "\n".join(lines) + "\n"


def _results_table(by_spread: Mapping[str, Mapping[str, Metrics]]) -> list[str]:
    baseline = by_spread[CURRENT.name][OVERALL].mrr_at_10
    rows = [
        "| spread | admissible | MRR@10 | Δ | Recall@5 | " + " | ".join(SIGNALS) + " |",
        "|---|:---:|---:|---:|---:|" + "---:|" * len(SIGNALS),
    ]
    for spread in CANDIDATES:
        metrics = by_spread[spread.name]
        overall = metrics[OVERALL]
        per_signal = " | ".join(f"{metrics[signal].mrr_at_10:.4f}" for signal in SIGNALS)
        rows.append(
            f"| {spread.name} | {'yes' if admissible(metrics) else 'no'} | {overall.mrr_at_10:.4f} | "
            f"{overall.mrr_at_10 - baseline:+.4f} | {overall.recall_at_5:.4f} | {per_signal} |"
        )
    return rows
