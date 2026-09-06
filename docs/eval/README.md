# Retrieval evaluation reports

Committed output of the measurement tooling in `tests/eval`. The gate itself
(`baseline.json`) lives next to the tests; this directory holds the reports
that are read by people rather than compared by a test.

| file | produced by | regenerate with |
|---|---|---|
| `ablation.md` | `tests/eval/test_ablation.py` | `SYNAPTO_TEST_PG_DSN=… SYNAPTO_EVAL_ABLATION=1 uv run pytest tests/eval` |

Every report names its run date, the corpus digest and the embedding provider,
so a number can always be traced to the inputs that produced it. Reports are
regenerated, never edited by hand; the reading below is the only prose that is.

## Reading of the 2026-09-06 ablation (#94)

Method, decision rule and the two blocks are documented in
`tests/eval/ablation.py`. Numbers are MRR@10 over the 55 golden cases with the
deterministic provider, so the vector leg is a lexical proxy and nothing here
speaks to semantic recall — the fusion machinery is what is measured.

| signal | removed from `full` | removed from `no-hrr` | verdict | follow-up |
|---|---:|---:|---|---|
| HRR boost | +0.3827 | — | hurts retrieval | [#103](https://github.com/ramonlimaramos/synapto/issues/103) |
| layer weight | +0.3413 | +0.0216 | hurts retrieval, even without the boost | [#104](https://github.com/ramonlimaramos/synapto/issues/104) |
| trust | −0.0619 | −0.0571 | earns its place | — |
| decay | −0.0059 (below floor) | −0.0545 | earns its place once the boost is gone | none filed, see below |
| vector leg | +0.3589 | −0.0238 | earns its place once the boost is gone | none filed, see below |
| keyword leg | −0.1194 | −0.3697 | earns its place | — |

Noise floors: 0.0097 for `full`, 0.0033 for `no-hrr` (spread of five
shuffled-insertion runs; only `created_at` tie-breaking differs between them).

**Why decay and the vector leg get no issue.** Both look expendable or harmful
under `full` and both earn their place under `no-hrr`. The difference is the
boost: at zero similarity it adds +0.075 to a relevance signal whose maximum is
≈0.033, so under `full` the order inside the candidate window is decided by the
multiplicative weights and any change to relevance looks like noise or like an
improvement. Their `full` verdicts describe the boost, not them. Both are
re-measured for free when #103 lands and the ablation is regenerated as part of
its acceptance.

**Why the layer weight does get one.** Its conditional delta (+0.0216) is six
times the `no-hrr` floor. The six `layer` cases need it (1.0 → 0.5 without),
the eighteen `general` cases pay for it (0.73 → 0.94 without): a 3× spread
lets a `core` memory with a weaker match outrank the memory that answers the
query. #104 chooses the spread by a sweep, after #103.
