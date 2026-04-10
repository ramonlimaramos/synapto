# Trust Scoring

Every memory in Synapto has a `trust_score` (0.0–1.0, default 0.5) that affects search ranking. Memories with higher trust appear first; low-trust memories sink.

## How It Works

Trust is adjusted via the `trust_feedback` MCP tool:

| Feedback | Delta | Rationale |
|----------|-------|-----------|
| `helpful=true` | +0.05 | Gradual reward for useful memories |
| `helpful=false` | -0.10 | Fast demotion for bad information |

The asymmetry is intentional — it takes 2 "helpful" votes to undo 1 "unhelpful" vote. Bad data gets cleaned up faster than good data accumulates.

## Impact on Search

Trust multiplies directly into the final search score:

```
score = rrf_score × decay_score × trust_score × depth_boost
```

A memory with `trust_score=0.3` ranks 40% lower than one with `trust_score=0.5`, all else being equal.

## MCP Usage

```
# boost a helpful memory
trust_feedback(memory_id="550e8400-e29b-41d4-a716-446655440000", helpful=true)

# penalize an inaccurate memory
trust_feedback(memory_id="550e8400-e29b-41d4-a716-446655440000", helpful=false)
```

## Python API

```python
# direct SQL if using as a library
await pg.execute(
    "UPDATE memories SET trust_score = GREATEST(0.0, LEAST(1.0, trust_score + %s)) WHERE id = %s;",
    (0.05, memory_id),
)
```

## Contradiction Detection

Trust scoring pairs naturally with `find_contradictions`. Workflow:

1. Run `find_contradictions` to find conflicting memory pairs
2. Review the pairs — decide which is correct
3. Use `trust_feedback(helpful=false)` on the incorrect one
4. Over time, bad memories sink below search thresholds

This creates a self-improving memory system where agent feedback continuously refines quality.
