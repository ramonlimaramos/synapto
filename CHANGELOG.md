# Changelog

All notable changes to Synapto will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `assert_release_contract.py` resolved its `git ls-remote` runner at definition time, so the command-line tests reached the real remote and broke as soon as the declared version had a published tag; the runner is now resolved at call time and the tests never leave the process

## [0.7.0] - 2026-09-04

### Added

- **write origin, recorded at write time and never inferred** (#77). Nothing
  said who authored a memory, so a rule the user typed and a rule an automated
  loop synthesized were indistinguishable once stored — tolerable only while
  every write is human. `memories.origin` (migration `009`) carries a closed
  `human | agent | consolidation` vocabulary, enforced by a `CHECK` as well as
  in Python so a raw write cannot invent a fourth origin no pruning rule knows
  how to treat. `remember(origin="agent")` lets an automated writer declare
  itself; the value is a parameter, never derived from the transport, the
  caller, or the content, following `hermes-agent`'s rule that the marker is
  "never inferred from location". Existing rows backfill as `human`, which is
  also simply true — no automated writer existed when they were written.
- **`recall(origin=...)` and `count_memories(origin=...)`**, so a loop can read
  back exactly what it wrote without re-ingesting the user's own rules. Indexed
  by `(tenant, origin)` on live rows.
- **`recall(metadata_filter=...)` and a match count that is not capped by
  `limit`** (#76). Memories carry an arbitrary `metadata` JSONB and nothing
  could query it, so "how many findings of `failure_class=x` exist?" could only
  be answered by recalling and counting client-side — a number bounded by the
  page size, which is a lower bound that silently stops being one as the store
  grows while still looking like a count. The filter translates to
  `metadata @> ...::jsonb` and composes with `tenant`, `depth_layer`,
  `subtype`, `domain`, and `scopes`, because it is built by the same
  `_build_memory_filters` the search uses — a count and a page cannot disagree
  about what matching means. `count_memories()` answers the aggregate question
  directly; `recall` reports it in its headline whenever a filter is given.
- **a GIN index on `memories.metadata`** (migration `008`), using
  `jsonb_path_ops` rather than the default operator class: it indexes whole
  paths instead of every key and value, which is smaller and faster for the
  containment operator this index exists to serve. The trade — no key-existence
  queries — is deliberate.
- **typed scopes are reachable from the MCP tools** (#75). The applicability
  axis was implemented and tested at four layers — the `memory_scopes` table,
  the `ScopeSet` value objects, the repository, and the search — and reachable
  from none of them, because `server.py` exposed only `domain`. A single string
  cannot express an intersection, so `domain=python` matched Python memories
  from every repository at once and `language:python ∧ repo:acme/api` had no
  path to a caller. `remember`, `recall`, and `update_memory` now accept
  `scopes` in the compact `"<type>:<key>"` form, `get_memory` and `recall`
  render them back in that same form, and `ScopeSet.parse` reads the compact
  spelling alongside the mapping form it already accepted. Splitting once on
  the first `:` is what keeps `repo:acme/api` intact; a scope with no type is
  rejected rather than guessed at, because `"python"` could be a language or a
  skill. Supplying both `domain` and `scopes` raises a `ToolError` naming both,
  and a non-canonical key raises with the canonical spelling named — the
  vocabulary's existing rejections, surfaced intact at the boundary instead of
  arriving as a repository traceback.
- **the tenant is derived from a resolved location instead of supplied by the
  caller** (#74). `remember(tenant=...)` accepted any string while the tenant is
  a hard partition key, so a memory written under one spelling was invisible to
  every read using another — silently, in both directions. The store had
  accumulated 29 tenants for about a dozen projects. `resolve_tenant()` now
  derives the tenant from the working directory's git `origin` remote as
  canonical `owner/name`, falling back to configuration and then `default`; an
  explicit tenant becomes an override that must arrive canonical and is
  rejected, never repaired, with the error naming the canonical spelling. The
  same resolution serves the MCP tools and the CLI, so `synapto search` and
  `recall` cannot disagree about which partition they read.
- **`tenant_aliases`** (migration `007`) records superseded spellings, so a
  tenant folded into another stays reachable. Resolution is exactly one hop:
  `TenantAliasRepository` refuses, under a lock, both directions of a chain,
  which keeps every read a single indexed lookup and makes a cycle impossible
  to create. Aliases are followed on writes as well as reads — writing under a
  spelling that was merged away would re-fragment what the merge consolidated.
- **`synapto maintain --merge-tenants`** reports how the stored tenants would
  collapse, and `--dry-run` is the default. Grouping is deterministic and
  purely lexical: identical-after-normalization spellings merge confidently, a
  single `owner/name` spelling among unqualified siblings is proposed for
  review, and a name claimed by two owners is printed and skipped rather than
  guessed at. Applying the plan moves real memories between partitions, so the
  canonical spelling is a human decision the tool declines to make.

### Changed

- **`forget` refuses a human-authored memory** (#77). Deleting one now requires
  `allow_human=true`, passed per call, and the deletion is logged as an
  explicit override. An agent following a heuristic will not pass the flag; a
  person answering "yes, delete mine" will. `forget` had no test coverage at
  all before this — worth noting, since it is the one tool whose failure mode
  is unrecoverable.
- **a `metadata_filter` is one level of scalars, and nesting is rejected**
  (#76). `@>` matches sub-objects and treats arrays as subsets, so a nested
  filter would answer something subtler than the exact-key equality the
  aggregation case needs, and the caller would have to know which. One level
  has exactly one reading; anything else raises a `ToolError` naming the
  offending key.
- **`domain` is deprecated in the tool docstrings and unchanged in behaviour**
  (#75). Every memory written so far uses it, so it stays accepted, stays in
  the input schemas, and keeps filtering exactly as before.
- **an absent tenant is no longer always `default`.** This is the intended
  behaviour change of #74 and it is visible: a process running inside a
  checkout now resolves that repository's `owner/name`. Memories previously
  written under a different spelling of the same project are unreachable until
  an alias records the merge — which is what `--merge-tenants` exists to
  arrange, and why it should be run before relying on the new default.
- **`bandit` skips `B404` and `B603`** for the single `subprocess` call that
  reads the git remote. The argv is built in-process, never reaches a shell,
  and carries no caller-supplied element in an executable position.

## [0.6.0] - 2026-09-04

### Added

- **typed scopes persisted and queried end to end** (groundwork for #45/#46/#47, none of which this closes): `MemoryRepository.create` accepts a `ScopeSet` and commits the memory with its memberships in one transaction; `update_with_scopes` updates fields and memberships in one transaction; `replace_scopes`/`clear_scopes` authorize the parent by tenant and active state under a `FOR UPDATE` lock before reaching the ID-only `ScopeRepository`, with `None` preserving, `[]` clearing, and a non-empty set replacing. Supplying `domain` and `scopes` together is rejected. `get_by_id`/`get_by_ids` carry ordered scopes, with `get_by_ids` normalizing ids to UUID and returning rows in the requested order, batched so a result page costs one extra query rather than one per memory. `hybrid_search` and `vector_search` accept a `scopes` filter implementing the Option B applicability rule — OR within a scope type, AND across the types a memory carries, extra query types imposing nothing, `global:all` always matching, unscoped memories excluded whenever a filter is given — expressed as `EXISTS`/`NOT EXISTS` so ranking is never duplicated. Filters are validated before embedding, so an invalid scope costs no model call and no query. `SearchResult` carries its scopes on both search paths, and `ScopeSet.to_payload`/`from_payload` give caches a deterministic representation that still reads pre-scope entries.
- **typed memory-scope contract** (groundwork for #45/#46/#47, none of which this closes): a new `memory_scopes` table gives memories an N:N applicability axis of typed `(scope_type, scope_key)` pairs — `repo:owner/name`, `language:python`, `skill:jerry-workday` — superseding the scalar `domain` string. `ScopeRef` and `ScopeSet` are immutable, deterministically ordered value objects that validate the six accepted types, require `global` to carry only `all` and never combine with other scopes, require `repo` keys in canonical `owner/repo` form, and cap a request at 20 unique scopes. Keys must arrive canonical (ASCII lowercase, already trimmed) and are rejected rather than silently rewritten, with the error naming the canonical spelling when one exists. `ScopeRepository` replaces and clears membership atomically under a `FOR UPDATE` lock on the parent memory, exposes connection-scoped primitives so a caller can commit a memory and its scopes in one transaction, and reads one or many memories' scopes ordered by `(scope_type, scope_key)`. Migration 005 and existing `domain` values are untouched; MCP tools, `MemoryRepository`, search, and the CLI are unchanged in this PR.
- **domain-scoped memory contract** (#45, #46, #47 foundation): memories gain a first-class nullable `domain` column (skill/repo/language bounded context) with a partial `(tenant, domain)` index. `MemoryRepository.create` persists domain; hybrid and vector search accept an injection-safe `domain` filter; `get_memory`/`get_memories`/`recall` output renders `domain` when set; CLI `search --domain`, `export`, and `import` preserve the field. Existing memories without domain keep working; MCP tool input parameters are unchanged (exposed in a follow-up PR).
- **domain-aware `remember` and `recall` MCP tools** (#45, #46 foundation): both tools accept an optional `domain` parameter (validated, max 50 chars) so agents can store `domain=python` / `domain=jerry-workday` context and recall it by bounded context without semantic query guessing. Tool descriptions and server instructions now guide agents to route durable skill/domain knowledge into Synapto with `domain=`. `alwaysLoad` stays limited to `remember`/`recall`.
- **typed memory-scope contract** (groundwork for #45/#46/#47, none of which this closes): a new `memory_scopes` table gives memories an N:N applicability axis of typed `(scope_type, scope_key)` pairs — `repo:owner/name`, `language:python`, `skill:jerry-workday` — superseding the scalar `domain` string. `ScopeRef` and `ScopeSet` are immutable, deterministically ordered value objects that validate the six accepted types, require `global` to carry only `all` and never combine with other scopes, require `repo` keys in canonical `owner/repo` form, and cap a request at 20 unique scopes. Keys must arrive canonical (ASCII lowercase, already trimmed) and are rejected rather than silently rewritten, with the error naming the canonical spelling when one exists. `ScopeRepository` replaces and clears membership atomically under a `FOR UPDATE` lock on the parent memory, exposes connection-scoped primitives so a caller can commit a memory and its scopes in one transaction, and reads one or many memories' scopes ordered by `(scope_type, scope_key)`. Migration 005 and existing `domain` values are untouched; MCP tools, `MemoryRepository`, search, and the CLI are unchanged in this PR.
- **domain-scoped memory contract** (#45, #46, #47 foundation): memories gain a first-class nullable `domain` column (skill/repo/language bounded context) with a partial `(tenant, domain)` index. `MemoryRepository.create` persists domain; hybrid and vector search accept an injection-safe `domain` filter; `get_memory`/`get_memories`/`recall` output renders `domain` when set; CLI `search --domain`, `export`, and `import` preserve the field. Existing memories without domain keep working; MCP tool input parameters are unchanged (exposed in a follow-up PR).
- **domain-aware `remember` and `recall` MCP tools** (#45, #46 foundation): both tools accept an optional `domain` parameter (validated, max 50 chars) so agents can store `domain=python` / `domain=jerry-workday` context and recall it by bounded context without semantic query guessing. Tool descriptions and server instructions now guide agents to route durable skill/domain knowledge into Synapto with `domain=`. `alwaysLoad` stays limited to `remember`/`recall`.
- **domain-scoped memory contract** (#45, #46, #47 foundation): memories gain a first-class nullable `domain` column (skill/repo/language bounded context) with a partial `(tenant, domain)` index. `MemoryRepository.create` persists domain; hybrid and vector search accept an injection-safe `domain` filter; `get_memory`/`get_memories`/`recall` output renders `domain` when set; CLI `search --domain`, `export`, and `import` preserve the field. Existing memories without domain keep working; MCP tool input parameters are unchanged (exposed in a follow-up PR).
- **domain-aware `remember` and `recall` MCP tools** (#45, #46 foundation): both tools accept an optional `domain` parameter (validated, max 50 chars) so agents can store `domain=python` / `domain=jerry-workday` context and recall it by bounded context without semantic query guessing. Tool descriptions and server instructions now guide agents to route durable skill/domain knowledge into Synapto with `domain=`. `alwaysLoad` stays limited to `remember`/`recall`.
- **domain-aware `remember` and `recall` MCP tools** (#45, #46 foundation): both tools accept an optional `domain` parameter (validated, max 50 chars) so agents can store `domain=python` / `domain=jerry-workday` context and recall it by bounded context without semantic query guessing. Tool descriptions and server instructions now guide agents to route durable skill/domain knowledge into Synapto with `domain=`. `alwaysLoad` stays limited to `remember`/`recall`.

### Changed

- **The 0.5 maintenance line is back on `main`.** `v0.5.1` was cut from
  `release/0.5` and never merged back, so each line held work the other
  lacked: `main` had the domain and typed-scope contracts without the
  packaging hardening, and the published wheel had the hardening without the
  scopes. Migrations `005` and `006` moved into the packaged
  `src/synapto/_migrations/`, which is what makes them travel in a
  distribution at all — without this, 0.6.0 would have shipped the same empty
  bundle that 0.1.0 through 0.5.0 did. Existing migration checksums are
  unchanged, so no database is orphaned. CI keeps `main`'s unexempted audit
  and its isolated test DSN, and adopts the release line's `package` job,
  which verifies both built artifacts rather than the source tree.
- **The workflow no longer writes the version it publishes** (#78). The former
  `bump_type` input edited `pyproject.toml` and `src/synapto/__init__.py` but
  not `uv.lock`, so any `patch`/`minor`/`major` dispatch produced `X`, `X`,
  `X-1` and died on the three-way consistency assert it was paired with; it
  also left `CHANGELOG.md` untouched, which Keep a Changelog does not allow
  for. A version is now a reviewed decision that arrives by pull request with
  its changelog entry and a regenerated lockfile, and the workflow publishes
  what `main` already declares — which is what makes the assert meaningful.
  The check itself moved to `scripts/assert_release_contract.py`, so its
  failure paths are covered by the suite instead of being reachable only by
  attempting a real release, and it now also refuses a version whose tag
  already points at different code, before anything is built.

### Fixed

- **`main` can release again** (#78). The back-merge resolved
  `.github/workflows/release.yml` in favour of the `release/0.5` side, so
  `main` carried the maintenance workflow pinned to `EXPECTED_REF:
  refs/heads/release/0.5` and `EXPECTED_VERSION: "0.5.1"`. Both are asserted
  before the build step, so every dispatch from `main` failed without
  producing an artifact — leaving 0.6.0, which contains the migration-bundling
  fix, cut and unpublishable. The generic workflow is restored with the
  packaging gates the v0.5.1 line added intact: migrations verified in both
  the wheel and the sdist, the four-job split that keeps `contents: write` and
  `id-token: write` out of the building job, and tagging ahead of the
  immutable PyPI upload. The ref pin, the version constant, and the
  `release-v0.5` concurrency group are gone.
- **positional-argument compatibility restored** for `MemoryRepository.create`, `hybrid_search`, and `vector_search`. The scalar `domain` parameter had been inserted mid-signature — between `subtype` and `summary`, and ahead of `limit` — so a positional caller's summary or limit was silently rebound to it. `domain` and the new `scopes` are now keyword-only, after every established positional parameter.

### Security

- **transformers constrained to `>=5.10.0`** for CVE-2026-9856, which turned
  `main` CI red as soon as the advisory landed — the resolved `5.5.3` came in
  transitively through `sentence-transformers`, which does not itself pin a
  fixed range. Resolves to `5.16.1`, carrying `tokenizers` to `0.23.2` and
  `safetensors` to `0.8.0`. `sentence-transformers` stays at `5.4.0` and the
  embedding model still loads at `dim=384`, so no stored vector is affected.
  The audit continues to run with no exemptions.
- **cryptography constrained to >=50.0.0** to fix CVE-2026-69247, which turned main CI red immediately after the PR-2 merge. Pulled transitively by authlib/joserfc.
- **dependency audit fixes** for advisories that appeared after v0.5.0: `click>=8.3.3` (PYSEC-2026-2132), `mcp>=1.28.1` (PYSEC-2026-3481/3482/3483, pulled transitively by fastmcp), and `setuptools>=83.0.0` (PYSEC-2026-3447). The setuptools floor also moves `torch` to 2.13, which resolves CVE-2025-3000 — so the `--ignore-vuln CVE-2025-3000` exemption was removed and CI now audits the full dependency tree with no exclusions.
- **dependency audit fixes** for advisories that appeared after v0.5.0: `click>=8.3.3` (PYSEC-2026-2132), `mcp>=1.28.1` (PYSEC-2026-3481/3482/3483, pulled transitively by fastmcp), and `setuptools>=83.0.0` (PYSEC-2026-3447). The setuptools floor also moves `torch` to 2.13, which resolves CVE-2025-3000 — so the `--ignore-vuln CVE-2025-3000` exemption was removed and CI now audits the full dependency tree with no exclusions.
- Raised the dev-environment `pip` floor to `26.2` for PYSEC-2026-3721. The
  audit now runs with no exemptions on this line: the `setuptools>=83` and
  torch pins that forced two temporary exclusions on the v0.5 maintenance
  branch are already satisfied here.

## [0.5.1] - 2026-08-07

### Fixed

- **SQL migrations are now bundled in published distributions.** Every wheel from 0.1.0 through 0.5.0 contained zero `.sql` files: the migrations lived in a repository-root `migrations/` directory that the wheel never packaged, so a clean `pip`/`uvx` install discovered nothing and reported success against an empty schema. `synapto init` created no tables. The files now live in `src/synapto/_migrations/` and are read through `importlib.resources`, so discovery works in source checkouts, editable installs, wheels, and zipped distributions alike. Migration filenames and bytes are unchanged, so existing databases keep matching on filename and checksum.
- **Migration discovery fails closed.** A missing, unreadable, empty, or malformed bundle now raises `MigrationDiscoveryError` before any database call, in `migrate_up`, `migrate_down`, `get_migration_status`, and `run_migrations` — including before the legacy `synapto_schema` bridge, which swallows exceptions. Discovery also no longer falls back to `cwd/migrations`, which could read unrelated SQL from whatever directory the process happened to run in.

### Added

- **`scripts/verify_wheel.py`**, an artifact gate that installs a built wheel into a throwaway environment outside the repository and asserts that migration discovery returns the expected files and checksums, ignoring any foreign `cwd/migrations`. It checks the wheel *and* the sdist, runs in CI on Python 3.11, and runs in the release workflow immediately after build — before the tag, the PyPI upload, and the GitHub release — so a distribution without migrations cannot be published again. The release jobs are ordered build → tag → publish → github_release, so the recoverable mutation happens before the immutable one.

### Security

- Backported secure minimums: `click>=8.3.3` (PYSEC-2026-2132), `mcp>=1.28.1` (PYSEC-2026-3481/3482/3483), and `cryptography>=50.0.0` (CVE-2026-69247).
- Two audit exemptions remain, deliberately: `CVE-2025-3000` (Torch 2.11) and `PYSEC-2026-3447` (setuptools 81). Resolving them requires Torch 2.13 and `setuptools>=83`, which raise the Apple Silicon wheel floor to macOS 14. That is a platform decision for the v0.6 line, not something a patch release should change silently. **These exemptions expire with that decision.**

## [0.5.0] - 2026-07-01

### Added

- **optional memory subtype filtering** for governed context domains: `remember(..., subtype=...)` can persist free-form memory subcategories, and `recall(..., subtype=...)`, hybrid search, and vector search can filter by subtype while preserving tenant and depth-layer filtering (#58).
- **automatic memory capture guidance** in MCP server instructions and tool metadata so agents get clearer direction on when to recall or store durable project, user, feedback, and reference memories (#57).

### Changed

- **Claude Code MCP configuration disables native flat-file auto-memory** by default for Synapto installs, keeping Synapto as the governed source of truth instead of duplicating memories into Claude's local memory files (#56).
- **GitHub Actions workflows updated for Node 24 compatibility**, including current checkout/setup actions used by CI and release automation (#55).

### Fixed

- **MCP stdio transport friendliness for long-running agent sessions** by adding a lightweight `ping` health tool, allowing sentence-transformers device override via `SYNAPTO_EMBEDDING_DEVICE` / `[embeddings].device`, disabling sentence-transformers encode progress bars, and ensuring CLI/MCP embedding provider paths share the same model/device configuration.
- **embedding tests now run offline and deterministically**, avoiding Hugging Face/cache network flakes in CI while still exercising the embedding integration surface (#59).

## [0.4.0] - 2026-06-10

### Added

- **incremental MCP memory updates** via `update_memory`, enabling agents to append to memory content, replace memory content, or patch memory metadata without re-sending the full record. Content updates refresh content-derived embedding, HRR, and entity links, while metadata-only updates avoid unnecessary recomputation.

### Security

- **dependency audit fixes** by constraining vulnerable transitive dependencies and dev-audit tooling dependencies: `idna>=3.15`, `starlette>=1.0.1`, `PyJWT>=2.13.0`, and `pip>=26.1.2`.

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
