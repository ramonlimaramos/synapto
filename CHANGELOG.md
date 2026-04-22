# Changelog

All notable changes to Synapto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **server instructions** injected into MCP clients via `FastMCP(instructions=...)` so the LLM knows when to call `recall` and `remember` without requiring manual CLAUDE.md configuration (#11)
- **`alwaysLoad` tool metadata** on `remember` and `recall` so Claude Code loads their schemas eagerly and skips the deferred `ToolSearch` round-trip — reduces first-call latency while keeping the other eight tools deferred (#15)
- **`<system-reminder>` wrapping on `recall` output** so Claude Code folds recalled memories into the conversation as contextual hints rather than verbatim tool output. The preamble and empty-state copy live in `prompts/recall_preamble.md` and `prompts/recall_empty.md` (reuses the `load_prompt` helper added in #11) (#16)

### Documentation

- **memory type alignment with Claude Code** — `docs/claude-code.md` now documents that Synapto's `user`, `feedback`, `project`, and `reference` types are a direct 1:1 match with Claude Code's native auto-memory types, enabling zero-transformation import of existing memories (#12)

## [0.1.0] - 2026-04-13

### Added

- **MCP server** with 10 tools: remember, recall, relate, forget, trust_feedback, find_contradictions, graph_query, list_entities, memory_stats, maintain
- **3-way hybrid search** combining vector similarity (pgvector HNSW), full-text (tsvector + BM25), and HRR compositional algebra via Reciprocal Rank Fusion
- **holographic reduced representations (HRR)** for compositional memory queries — probe, reason, and contradict operations that no vector database can do
- **knowledge graph** with automatic entity extraction, directed relations, and N-hop recursive CTE traversal
- **time-based decay** with 4 depth layers: core (forever), stable (~6 months), working (~1 week), ephemeral (~6 hours)
- **trust scoring** with asymmetric feedback: helpful +0.05, unhelpful -0.10 for self-cleaning memory
- **memory banks** using HRR bundled superpositions for O(1) category-level queries
- **repository pattern** isolating all SQL into dedicated repository classes — zero raw SQL in business logic
- **CLI** with commands: serve, init, search, stats, doctor, migrate (up/down/status), export, import
- **interactive init** (`synapto init --interactive`) with MCP client auto-detection and uvx config
- **multi-tenant isolation** via tenant scoping on all tables
- **versioned SQL migrations** with up/down support and checksum validation
- **embedding providers**: sentence-transformers (CPU default) and OpenAI
- **CI pipeline** with lint (ruff), security (bandit), dependency audit (pip-audit), and tests across Python 3.11/3.12/3.13
- **documentation** for Claude Code, Cursor, LangGraph, Agno integration
- **docker compose** setup for quick start
- **uvx support** as recommended installation method for automatic updates

### Security

- all SQL queries use parameterized placeholders (no string interpolation)
- bandit static analysis integrated in CI
- pip-audit dependency scanning in CI
