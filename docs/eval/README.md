# Retrieval evaluation reports

Committed output of the measurement tooling in `tests/eval`. The gate itself
(`baseline.json`) lives next to the tests; this directory holds the reports
that are read by people rather than compared by a test.

| file | produced by | regenerate with |
|---|---|---|
| `ablation.md` | `tests/eval/test_ablation.py` | `SYNAPTO_TEST_PG_DSN=… SYNAPTO_EVAL_ABLATION=1 uv run pytest tests/eval` |
| `layer_sweep.md` | `tests/eval/test_layer_sweep.py` | `SYNAPTO_TEST_PG_DSN=… SYNAPTO_EVAL_LAYER_SWEEP=1 uv run pytest tests/eval` |

Every report names its run date, the corpus digest and the embedding provider,
so a number can always be traced to the inputs that produced it. Reports are
regenerated, never edited by hand; the reading below is the only prose that is.

## Reading of the 2026-09-06 layer-weight sweep (#104)

Method and decision rule are documented in `tests/eval/layer_sweep.py`; the
table is `layer_sweep.md`. Ten spreads, read `core/stable/working/ephemeral`,
each run over the same seeded corpus under `full` and under `no-hrr`. A spread
is admissible when the six `layer` cases all rank their target first; among
admissible spreads the best overall MRR@10 wins, and a tie within the noise
floor goes to the narrower one.

| spread | admissible | MRR@10 under `full` | `general` | `hrr` |
|---|:---:|---:|---:|---:|
| 1.5/1.2/1.0/0.5 (until 0.8.0) | no — `layer` 0.9167 | 0.9208 | 0.8333 | 0.8571 |
| 1.5/1.3/1.0/0.5 | yes | 0.9274 | 0.8259 | 0.8571 |
| 1.4/1.3/1.0/0.7 | yes | 0.9279 | 0.8259 | 0.8611 |
| **1.3/1.25/1.0/0.7 (chosen)** | yes | **0.9482** | 0.8861 | 0.8667 |
| 1.3/1.15/1.0/0.7 | no — `layer` 0.9167 | 0.9238 | 0.8426 | 0.8571 |
| 1.2/1.1/1.0/0.8 and narrower | no — `layer` ≤ 0.75 | 0.9061 – 0.9218 | up to 0.9167 | 0.8667 |
| 1.0/1.0/1.0/1.0 (flat) | no — `layer` 0.5 | 0.9036 | 0.9167 | 0.8667 |

**The verdict.** `1.3/1.25/1.0/0.7` is the only spread that is both admissible
and best: +0.0274 overall over the previous production value against a noise
floor of 0.0000 (five shuffled reseeds agree to the fourth digit), Recall@5
0.9818 → 1.0, `layer` 0.9167 → 1.0, `general` 0.8333 → 0.8861, no slice worse.
The ablation regenerated under it reads "earns its place" for the layer weight
in both blocks (−0.0445 under `full`, −0.0273 under `no-hrr`); the conditional
verdict from the reading above is gone.

**Why narrowing alone did not work.** The issue's premise was that a 3× spread
was too wide, and the `general` column agrees — it rises monotonically as the
spread narrows. But every spread narrower than the chosen one misses
`layer-04`, and the reason is the HRR leg: a `working` memory that restates the
query verbatim earns evidence 1.0, a full third leg, so the `stable` version
that answers the same question needs about 1.25× over `working` to stay first.
The binding ratio is `stable / working`, not the overall width. Within that
constraint the rest was chosen narrow: `core` sits just above `stable` because
no golden case asks them to compete, and `ephemeral` 0.7 sinks a scratch note
below `working` at equal relevance without burying it.

**What the `no-hrr` column says.** Without the HRR leg the picture inverts:
every spread down to `1.05/1.02/1.0/0.95` is admissible and the narrowest wins,
0.9561, above the 0.9482 of `full`. That is the HRR leg and the layer weight
pulling against each other on this corpus — the leg rewards verbatim
restatement, the weight promotes the authoritative version — and it is a
question about the leg, not about the spread. It is left as a follow-up rather
than decided here: the leg earns its place in the ablation (−0.0027, which is
also the smallest margin in the table), and the golden set's `hrr` slice is
too small to settle it.

## Reading of the 2026-09-06 ablation, after #103 and before #104

Method, decision rule and the two blocks are documented in
`tests/eval/ablation.py`. Numbers are MRR@10 over the 55 golden cases with the
deterministic provider, so the vector leg is a lexical proxy and nothing here
speaks to semantic recall — the fusion machinery is what is measured.

| signal | removed from `full` | removed from `no-hrr` | verdict | follow-up |
|---|---:|---:|---|---|
| HRR leg | −0.0242 | — | earns its place | — |
| decay | −0.0561 | −0.0545 | earns its place | — |
| trust | −0.0488 | −0.0571 | earns its place | — |
| layer weight | −0.0171 | +0.0216 | earns its place with the HRR leg; hurts without it | [#104](https://github.com/ramonlimaramos/synapto/issues/104), reframed |
| vector leg | −0.0571 | −0.0238 | earns its place | — |
| keyword leg | −0.1733 | −0.3697 | earns its place | — |

Noise floors: 0.0030 for `full`, 0.0033 for `no-hrr`.

**What #103 changed.** The old boost had two defects, and the first hid the
second. It added `((sim + 1) / 2) × 0.15` to the RRF sum, so an unrelated memory
gained +0.075 against a largest possible RRF sum of ≈0.033. And it compared an
unbound `encode_text(query)` with a memory stored as `encode_fact(content,
entities)` — a role-bound vector — which is noise by construction: no golden
case ranked its target first on HRR similarity alone. The leg now encodes the
query as `remember` encodes the memory and adds `evidence / (k + 1)`, where
evidence is the similarity above the noise floor scaled to [0, 1]. Overall
MRR@10 went from 0.5138 to 0.9208; `no-hrr` stays at 0.8965, so the leg is
worth +0.024 over not having it.

**Why not a third reciprocal-rank leg.** It was the recommended option in #103
and it was measured first: 0.8771 overall, below `no-hrr`. A twenty-row window
ranks densely, so the candidate the HRR leg happens to rank first — often at
0.15 similarity — receives a full leg, the same as a perfect match. Credit
proportional to the evidence scored 0.9208 with the same probe. Both numbers
are in the #103 pull request.

**Where the layer weight stood.** With the HRR leg in place the layer weight
earned its place (−0.0171 against a 0.0030 floor); its conditional verdict
(+0.0216 without HRR) was unchanged. The `layer` slice was 0.9167 rather than
1.0000, one case where a `working` twin outranked the `stable` target. #104
kept its question — is 0.5–1.5 the right spread — with the premise "could be
better" rather than "hurts"; the sweep above answered it.

## Reading of the 2026-09-06 ablation before #103 (#94)

Kept for the record; superseded by the reading above.

| signal | removed from `full` | removed from `no-hrr` | verdict then |
|---|---:|---:|---|
| HRR boost | +0.3827 | — | hurt retrieval |
| layer weight | +0.3413 | +0.0216 | hurt retrieval, even without the boost |
| trust | −0.0619 | −0.0571 | earned its place |
| decay | −0.0059 (below floor) | −0.0545 | earned its place once the boost was gone |
| vector leg | +0.3589 | −0.0238 | earned its place once the boost was gone |
| keyword leg | −0.1194 | −0.3697 | earned its place |

Noise floors then: 0.0097 for `full`, 0.0033 for `no-hrr`. The `full` verdicts
for decay, layer and the vector leg described the boost, not them: at zero
similarity it added +0.075 to a relevance signal whose maximum was ≈0.033, so
the order inside the candidate window was decided by the multiplicative
weights.
