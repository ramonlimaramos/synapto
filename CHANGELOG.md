# Changelog

All notable changes to Synapto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-05-11

### Added

- **cross-agent handoff prompts and template tools** for coordinating Codex, Claude Code, Cursor, and other MCP clients through metadata-tagged project memories, plus documentation for the natural-language handoff UX, schema, and two-stage retrieval workflow.

## [0.2.1] - 2026-05-06

### Added

- **Claude Code memory migration parser** with auto-detection for imported native Claude Code memories, making `synapto import` easier to use with existing agent memory files.
- **structured JSON logging foundation** via `structlog` + `orjson`, giving Synapto consistent machine-readable stderr logs.
- **metrics primitives and instrumentation** including a process-wide registry, log backend, async timing helper, per-tool call counters, latency histograms, and a Postgres metrics backend for persisted tool telemetry.
- **full MCP memory retrieval** via `get_memory` and `get_memories`, enabling two-stage retrieval where `recall` can return compact previews and agents can fetch complete content only for selected memories.
- **configurable recall previews** with `preview_chars`, plus `tenant` and `created_at` in recall output so agents can disambiguate results before fetching full records.

### Fixed

- **FastMCP startup banner suppression** for `synapto serve`; the Rich banner and update notice no longer pollute stderr with non-JSON lines, preserving structured log hygiene for MCP stdio deployments (#28).
- **Postgres telemetry backend shutdown behavior** now drains or cancels in-flight writes before the pool closes, rejects late emits after shutdown, and resets the process registry during server teardown.
- **metrics retention purge efficiency** by using the database cursor row count instead of materializing every deleted metric ID in Python.
- **deterministic metric listing** by ordering equal timestamps with `id DESC`.
- **migration test isolation** for temporary test migrations, avoiding stale local rows from interrupted test runs.

### Security

- **CI dependency audit stability** by constraining `pip>=26.1` in the development extras to avoid the known vulnerable pip version bundled by older uv-created environments.

### Documentation

- **install snippets use `uvx --refresh`** so users actually receive new releases on Claude Code / Cursor restart — the previous snippets relied on `uvx` reusing its package cache, which meant a published Synapto release could go unnoticed until the cache expired. README, `docs/claude-code.md`, and `docs/cursor.md` now recommend the `--refresh` flag, document the startup tradeoff, and describe the manual `uv cache clean synapto` escape hatch. Also explains why a full Claude Code quit is required to pick up a new version mid-session.
- **release notes auto-update snippet** now also recommends `uvx --refresh`, matching the install docs.

## [0.2.0] - 2026-04-22

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
