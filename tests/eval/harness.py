"""Golden-set retrieval harness — the corpus, the cases, the metrics and the baseline contract.

Retrieval quality was unmeasured: the unit suite proves that filters apply and
that results are non-empty, never that the *right* memory comes back. The
ranking bug fixed in #87 (three signals silently dropped from the final order)
was invisible to every test for six releases. This harness is the smallest
in-repo measurement that would have caught it.

**What is measured.** Each case names a query, the corpus memory that must come
back, and the filters the call carries. Two metrics, both standard and both
computable without a dependency: MRR@10 (the reciprocal of the expected
memory's rank, zero past the cutoff) and Recall@5 (whether it is in the first
five). They are reported per signal — ``layer``, ``trust``, ``decay``, ``hrr``,
``scopes``, ``metadata``, ``general`` — so a regression names the mechanism it
broke, and so the ablation in #94 has a per-signal slice to ablate against.

**The baseline is a measurement, not an aspiration.** ``baseline.json`` records
what the current machinery scores; a case the machinery gets wrong today is
kept, with its low score, rather than removed to make the number pretty. The
gate is symmetric: a drop beyond :data:`TOLERANCE` fails as a regression, and a
rise beyond it fails too, asking for a re-baseline in the same commit — a
baseline that lags the truth in either direction stops being one.

**Why the deterministic provider.** The gate runs on the same hermetic,
hash-of-tokens embeddings the unit suite uses, so CI needs no model download
and the numbers are reproducible byte for byte. That makes the vector leg a
proxy for lexical overlap, which is exactly enough to measure the fusion
machinery — weights, HRR boost, scope and metadata filters — and not enough to
say anything about semantic recall. The provider name is recorded in the
baseline so a run with a different provider is never compared against it.

**Why HRR cases do not isolate HRR.** The boost compares an unbound bag-of-words
query against a role-bound fact, so no pair of memories can be constructed
where HRR is the deciding signal in a known direction. The ``hrr`` cases cover
the territory the signal claims — multi-entity, structural queries — and leave
the question of whether it contributes to #94.

**Layout.** ``corpus.toml`` holds every memory under tenant ``acme/eval``, each
with a stable ``key`` the cases refer to. ``golden/<signal>.toml`` holds the
cases; the file stem is the signal. Content is synthetic ``acme/*`` material
only.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).parent
CORPUS_PATH = EVAL_DIR / "corpus.toml"
GOLDEN_DIR = EVAL_DIR / "golden"
BASELINE_PATH = EVAL_DIR / "baseline.json"

TENANT = "acme/eval"
MRR_CUTOFF = 10
RECALL_CUTOFF = 5
TOLERANCE = 0.02
SIGNALS = ("layer", "trust", "decay", "hrr", "scopes", "metadata", "general")
MIN_CASES_PER_SIGNAL = 5
OVERALL = "overall"


class GoldenSetError(ValueError):
    """The corpus or the cases are internally inconsistent."""


@dataclass(frozen=True)
class Memory:
    key: str
    content: str
    memory_type: str = "project"
    depth_layer: str = "stable"
    scopes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    trust: float | None = None
    decay: float | None = None


@dataclass(frozen=True)
class Case:
    signal: str
    query: str
    expected: str
    scopes: tuple[str, ...] = ()
    metadata_filter: dict[str, Any] | None = None
    depth_layer: str | None = None


@dataclass(frozen=True)
class Outcome:
    case: Case
    ranked: tuple[str, ...]

    @property
    def reciprocal_rank(self) -> float:
        return reciprocal_rank(self.ranked, self.case.expected, MRR_CUTOFF)

    @property
    def recalled(self) -> bool:
        return self.case.expected in self.ranked[:RECALL_CUTOFF]


@dataclass(frozen=True)
class Metrics:
    mrr_at_10: float
    recall_at_5: float
    cases: int


def load_corpus(path: Path = CORPUS_PATH) -> list[Memory]:
    """Read the corpus; keys must be unique because cases address memories by key."""
    entries = tomllib.loads(path.read_text())["memory"]
    memories = [
        Memory(
            key=entry["key"],
            content=entry["content"],
            memory_type=entry.get("type", "project"),
            depth_layer=entry.get("layer", "stable"),
            scopes=tuple(entry.get("scopes", ())),
            metadata=dict(entry.get("metadata", {})),
            trust=entry.get("trust"),
            decay=entry.get("decay"),
        )
        for entry in entries
    ]
    duplicates = _duplicates(memory.key for memory in memories)
    if duplicates:
        raise GoldenSetError(f"duplicate corpus keys: {sorted(duplicates)}")
    return memories


def load_cases(directory: Path = GOLDEN_DIR) -> list[Case]:
    """Read every ``golden/<signal>.toml``; the stem names the signal."""
    cases: list[Case] = []
    for path in sorted(directory.glob("*.toml")):
        signal = path.stem
        if signal not in SIGNALS:
            raise GoldenSetError(f"{path.name}: unknown signal {signal!r}; expected one of {SIGNALS}")
        for entry in tomllib.loads(path.read_text())["case"]:
            cases.append(
                Case(
                    signal=signal,
                    query=entry["query"],
                    expected=entry["expected"],
                    scopes=tuple(entry.get("scopes", ())),
                    metadata_filter=entry.get("metadata_filter"),
                    depth_layer=entry.get("depth_layer"),
                )
            )
    return cases


def check_consistency(corpus: Iterable[Memory], cases: Iterable[Case]) -> None:
    """Fail loudly on a case that points nowhere or a signal too thin to measure.

    Raises:
        GoldenSetError: an expected key is not in the corpus, a signal has fewer
            than :data:`MIN_CASES_PER_SIGNAL` cases, or a signal has none.
    """
    keys = {memory.key for memory in corpus}
    case_list = list(cases)
    missing = sorted({case.expected for case in case_list} - keys)
    if missing:
        raise GoldenSetError(f"cases expect keys absent from the corpus: {missing}")
    counts = {signal: sum(case.signal == signal for case in case_list) for signal in SIGNALS}
    thin = {signal: count for signal, count in counts.items() if count < MIN_CASES_PER_SIGNAL}
    if thin:
        raise GoldenSetError(f"signals with fewer than {MIN_CASES_PER_SIGNAL} cases: {thin}")


def corpus_digest(corpus_path: Path = CORPUS_PATH, golden_dir: Path = GOLDEN_DIR) -> str:
    """Content hash of the corpus and every case file, so a baseline names its inputs."""
    digest = hashlib.sha256()
    for path in [corpus_path, *sorted(golden_dir.glob("*.toml"))]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def reciprocal_rank(ranked: Iterable[str], expected: str, cutoff: int) -> float:
    """``1 / rank`` of ``expected`` within the first ``cutoff`` results, else 0."""
    for position, key in enumerate(list(ranked)[:cutoff], start=1):
        if key == expected:
            return 1.0 / position
    return 0.0


def summarize(outcomes: Iterable[Outcome]) -> dict[str, Metrics]:
    """Per-signal metrics plus an ``overall`` row over every case."""
    outcome_list = list(outcomes)
    summary = {OVERALL: _metrics(outcome_list)}
    for signal in SIGNALS:
        of_signal = [outcome for outcome in outcome_list if outcome.case.signal == signal]
        if of_signal:
            summary[signal] = _metrics(of_signal)
    return summary


def compare(observed: Mapping[str, Metrics], baseline: Mapping[str, Any]) -> list[str]:
    """Every metric that left the ``±TOLERANCE`` band around the baseline, as one line each.

    A drop is a regression. A rise is a baseline that no longer describes the
    machinery, and it fails with a different message so the fix is obvious: run
    with ``SYNAPTO_EVAL_WRITE_BASELINE=1`` and commit the result.
    """
    deviations = []
    recorded = baseline["metrics"]
    for name in [OVERALL, *SIGNALS]:
        if name not in observed or name not in recorded:
            continue
        for metric in ("mrr_at_10", "recall_at_5"):
            now = getattr(observed[name], metric)
            then = recorded[name][metric]
            if now < then - TOLERANCE:
                deviations.append(f"{name}.{metric} regressed: {then:.4f} → {now:.4f}")
            elif now > then + TOLERANCE:
                deviations.append(f"{name}.{metric} improved: {then:.4f} → {now:.4f} — re-baseline in this commit")
    return deviations


def render_baseline(observed: Mapping[str, Metrics], provider_name: str, digest: str) -> dict[str, Any]:
    return {
        "provider": provider_name,
        "corpus_digest": digest,
        "tolerance": TOLERANCE,
        "metrics": {name: asdict(metrics) for name, metrics in observed.items()},
    }


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_baseline(payload: Mapping[str, Any], path: Path = BASELINE_PATH) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def render_table(observed: Mapping[str, Metrics]) -> str:
    rows = [f"{'signal':<10} {'cases':>5} {'MRR@10':>8} {'Recall@5':>9}"]
    for name in [OVERALL, *SIGNALS]:
        if name in observed:
            metrics = observed[name]
            rows.append(f"{name:<10} {metrics.cases:>5} {metrics.mrr_at_10:>8.4f} {metrics.recall_at_5:>9.4f}")
    return "\n".join(rows)


def render_misses(outcomes: Iterable[Outcome]) -> str:
    """Each case whose expected memory did not rank first, with what did."""
    lines = []
    for outcome in outcomes:
        if outcome.ranked[:1] == (outcome.case.expected,):
            continue
        shown = ", ".join(outcome.ranked[:3]) or "<nothing>"
        lines.append(f"[{outcome.case.signal}] {outcome.case.query!r}: expected {outcome.case.expected}, got {shown}")
    return "\n".join(lines) or "every case ranked its expected memory first"


def _metrics(outcomes: list[Outcome]) -> Metrics:
    count = len(outcomes)
    return Metrics(
        mrr_at_10=sum(outcome.reciprocal_rank for outcome in outcomes) / count,
        recall_at_5=sum(outcome.recalled for outcome in outcomes) / count,
        cases=count,
    )


def _duplicates(items: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for item in items:
        if item in seen:
            repeated.add(item)
        seen.add(item)
    return repeated
