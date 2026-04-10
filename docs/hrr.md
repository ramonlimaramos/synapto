# Holographic Reduced Representations (HRR)

HRR adds compositional algebraic search to Synapto's hybrid engine. Unlike embeddings (which measure similarity) and full-text (which matches keywords), HRR encodes **structural relationships** between concepts and can extract them via algebra.

## What It Enables

| Operation | What it does | No other vector DB does this |
|-----------|-------------|------------------------------|
| `probe("kafka")` | Find memories where Kafka plays a structural role | Algebraic, not keyword match |
| `reason(["kafka", "hermes"])` | Find memories about BOTH entities simultaneously | Vector-space JOIN |
| `find_contradictions` | Detect memories with same entities but different claims | Automated memory hygiene |
| `trust_feedback` | Boost or penalize memory trust score | Asymmetric: +0.05 / -0.10 |

## How It Works

Each memory gets a **phase vector** (1024 angles in [0, 2π)) alongside its embedding:

```
remember("Hermes uses outbox pattern for Kafka")
  → embedding: [0.23, -0.41, ...]      # semantic similarity
  → hrr_vector: [1.42, 5.81, ...]      # compositional structure
```

The HRR vector encodes entities bound to roles:

```
fact = bundle(
    bind(encode_text("hermes uses outbox pattern for kafka"), ROLE_CONTENT),
    bind(encode_atom("hermes"), ROLE_ENTITY),
    bind(encode_atom("kafka"), ROLE_ENTITY),
)
```

Retrieval uses **unbinding** — algebraic extraction:

```
unbind(fact, bind("kafka", ROLE_ENTITY)) ≈ content_signal
```

## 3-Way Search

The `recall` tool now uses RRF across three signals:

```
final_score = (1/(k+semantic_rank) + 1/(k+keyword_rank) + hrr_boost) × decay × trust × depth_boost
```

If a memory has no HRR vector (pre-existing data), search gracefully falls back to 2-way RRF.

## MCP Tools

### trust_feedback

Adjust a memory's reliability score:

```
trust_feedback(memory_id="abc-123", helpful=true)   → trust: 0.50 → 0.55
trust_feedback(memory_id="abc-123", helpful=false)  → trust: 0.55 → 0.45
```

Asymmetric by design — bad memories get penalized 2× faster than good ones get rewarded.

### find_contradictions

Scan for memory pairs that share entities but disagree:

```
find_contradictions(tenant="myproject", threshold=0.3)
```

Returns pairs with a contradiction score based on:
- **Entity overlap** (Jaccard similarity of linked entities)
- **Content divergence** (low HRR vector similarity)

## Python API

```python
from synapto.hrr.core import encode_atom, bind, unbind, similarity, encode_fact
from synapto.hrr.retrieval import probe, reason, contradict
from synapto.hrr.banks import rebuild_tenant_banks

# compositional search: find memories where "kafka" is structural
results = await probe(pg, "kafka", tenant="myproject")

# multi-entity join: memories about kafka AND hermes together
results = await reason(pg, ["kafka", "hermes"], tenant="myproject")

# detect contradictions
pairs = await contradict(pg, tenant="myproject", threshold=0.3)

# rebuild category memory banks after bulk operations
await rebuild_tenant_banks(pg, "myproject")
```

## Core Algebra

```python
from synapto.hrr.core import encode_atom, bind, unbind, bundle, similarity

a = encode_atom("concept_a")   # deterministic via SHA-256
b = encode_atom("concept_b")

bound = bind(a, b)              # associate: result is dissimilar to both
recovered = unbind(bound, a)    # extract: similarity(recovered, b) > 0.8
merged = bundle(a, b)           # superpose: similar to both inputs

similarity(a, a)                # 1.0 (identical)
similarity(a, b)                # ~0.0 (unrelated)
```

## Capacity

HRR capacity is O(√dim). At dim=1024, ~256 items per bank before SNR degrades below 2.0. The system logs warnings when approaching capacity.
