# Synapto + Agno

Synapto integrates with [Agno](https://github.com/agno-agi/agno) agents as a memory backend.

## Setup

```bash
pip install synapto agno
```

## Usage

```python
import asyncio
from agno.agent import Agent
from agno.models.anthropic import Claude
from synapto.db.postgres import PostgresClient
from synapto.db.migrations import run_migrations, ensure_hnsw_index
from synapto.embeddings.registry import get_provider
from synapto.search.hybrid import hybrid_search


async def get_context(query: str, tenant: str = "default") -> str:
    """Fetch relevant memories from Synapto."""
    pg = PostgresClient("postgresql://localhost/synapto")
    await pg.connect()

    provider = get_provider()
    results = await hybrid_search(pg, provider, query, tenant=tenant, limit=5)
    await pg.close()

    if not results:
        return ""
    return "\n".join(f"- [{r.depth_layer}] {r.content}" for r in results)


# use as context in an Agno agent
agent = Agent(
    model=Claude(id="claude-sonnet-4-20250514"),
    description="An agent with persistent memory via Synapto",
    instructions=[
        "You have access to a memory graph. Use it to recall context.",
    ],
)
```

## As an Agno Tool

You can also expose Synapto operations as Agno tools for the agent to call directly. See the LangGraph integration guide for the tool pattern — the same `recall_memory` and `store_memory` functions work as Agno tools.
