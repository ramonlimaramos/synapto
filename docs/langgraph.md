# Synapto + LangGraph

Synapto can be used as a memory backend for LangGraph agents directly via its Python API.

## Setup

```bash
pip install synapto langgraph
```

## Usage as a LangGraph Tool

```python
from langchain_core.tools import tool
from synapto.db.postgres import PostgresClient
from synapto.db.migrations import run_migrations, ensure_hnsw_index
from synapto.embeddings.registry import get_provider
from synapto.search.hybrid import hybrid_search

# initialize once
pg = PostgresClient("postgresql://localhost/synapto")
provider = get_provider()


@tool
async def recall_memory(query: str, tenant: str = "default") -> str:
    """Search the memory graph for relevant context."""
    results = await hybrid_search(pg, provider, query, tenant=tenant, limit=5)
    if not results:
        return "No relevant memories found."
    return "\n".join(f"[{r.depth_layer}] {r.content}" for r in results)


@tool
async def store_memory(content: str, memory_type: str = "general", tenant: str = "default") -> str:
    """Store a piece of information in the memory graph."""
    from psycopg.types.json import Jsonb

    embedding = await provider.embed_one(content)
    await pg.execute(
        "INSERT INTO memories (content, embedding, embedding_dim, type, tenant) VALUES (%s, %s, %s, %s, %s);",
        (content, embedding, provider.dimension, memory_type, tenant),
    )
    return f"Stored: {content[:100]}"
```

## In a LangGraph StateGraph

```python
from langgraph.graph import StateGraph, MessagesState

graph = StateGraph(MessagesState)

# bind tools to your LLM
tools = [recall_memory, store_memory]
llm_with_tools = llm.bind_tools(tools)

# add nodes...
graph.add_node("agent", call_model)
graph.add_node("tools", tool_node)
```
