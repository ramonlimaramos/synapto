"""Synapto CLI — command-line interface for managing the memory graph."""

from __future__ import annotations

import asyncio
import json
import logging

import click

from synapto import __version__

logger = logging.getLogger("synapto.cli")

# Memory migration: how many texts to embed per provider call.
EMBEDDING_BATCH_SIZE = 64


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


@click.group()
@click.version_option(version=__version__, prog_name="synapto")
@click.option("--verbose", "-v", is_flag=True, help="enable debug logging")
def main(verbose: bool) -> None:
    """Synapto — persistent memory graph for AI coding agents."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option("--pg-dsn", envvar="SYNAPTO_PG_DSN", default="postgresql://localhost/synapto")
@click.option("--interactive", "-i", is_flag=True, help="interactively configure synapto")
def init(pg_dsn: str, interactive: bool) -> None:
    """Initialize the database — create tables, indexes, and extensions."""
    import tomli_w

    from synapto.config import CONFIG_DIR, CONFIG_FILE, save_default_config

    if interactive:
        click.echo("synapto interactive setup\n")

        pg_dsn = click.prompt("postgresql dsn", default=pg_dsn)
        redis_url = click.prompt("redis url", default="redis://localhost:6379/0")
        tenant = click.prompt("default tenant name", default="default")
        provider = click.prompt(
            "embedding provider",
            default="sentence-transformers",
            type=click.Choice(["sentence-transformers", "openai"], case_sensitive=False),
        )

        # write config
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "postgresql": {"dsn": pg_dsn},
            "redis": {"url": redis_url},
            "embeddings": {"provider": provider, "model": ""},
            "defaults": {"tenant": tenant},
            "decay": {"ephemeral_max_age_hours": 24, "purge_after_days": 30},
            "server": {"name": "synapto"},
        }
        with open(CONFIG_FILE, "wb") as f:
            tomli_w.dump(data, f)
        click.echo(f"\nconfig written: {CONFIG_FILE}")

        # run migrations
        async def _init_interactive():
            from synapto.db.migrations import get_schema_version, run_migrations
            from synapto.db.postgres import PostgresClient

            client = PostgresClient(pg_dsn)
            await client.connect()
            await run_migrations(client)
            version = await get_schema_version(client)
            await client.close()
            return version

        version = _run(_init_interactive())
        click.echo(f"database initialized (schema v{version})")

        # check embedding model availability
        click.echo(f"loading embedding model ({provider})...")
        from synapto.embeddings.registry import get_provider

        p = get_provider(provider)
        click.echo(f"embedding model ready: {p.name} (dim={p.dimension})")

        # offer to write MCP client config
        _offer_mcp_config(tenant)

        # memory migration
        _offer_memory_migration()

        # summary
        click.echo("\n--- setup complete ---")
        click.echo(f"  postgresql: {pg_dsn}")
        click.echo(f"  redis:      {redis_url}")
        click.echo(f"  tenant:     {tenant}")
        click.echo(f"  embeddings: {p.name}")
        click.echo(f"  config:     {CONFIG_FILE}")
        return

    config_path = save_default_config()
    click.echo(f"config: {config_path}")

    async def _init():
        from synapto.db.migrations import get_schema_version, run_migrations
        from synapto.db.postgres import PostgresClient

        client = PostgresClient(pg_dsn)
        await client.connect()

        version = await get_schema_version(client)
        if version:
            click.echo(f"schema already at version {version}, re-applying...")

        await run_migrations(client)
        version = await get_schema_version(client)
        click.echo(f"synapto database initialized (schema v{version})")

        await client.close()

    _run(_init())


@main.command()
def serve() -> None:
    """Start the Synapto MCP server (stdio transport)."""
    from synapto.server import mcp
    mcp.run()


@main.command()
@click.argument("query")
@click.option("--tenant", "-t", default=None, help="tenant/project scope")
@click.option("--limit", "-n", default=10, help="max results")
@click.option("--depth", "-d", default=None, help="depth layer filter")
def search(query: str, tenant: str | None, limit: int, depth: str | None) -> None:
    """Search memories from the command line."""

    async def _search():
        from synapto.config import load_config
        from synapto.db.migrations import ensure_hnsw_index
        from synapto.db.postgres import PostgresClient
        from synapto.embeddings.registry import get_provider
        from synapto.search.hybrid import hybrid_search

        config = load_config()
        t = tenant or config.default_tenant

        client = PostgresClient(config.pg_dsn)
        await client.connect()

        provider = get_provider(config.embedding_provider)
        await ensure_hnsw_index(client, provider.dimension)

        results = await hybrid_search(
            client, provider, query, tenant=t, depth_layer=depth, limit=limit
        )

        if not results:
            click.echo("no memories found")
        else:
            for r in results:
                click.echo(f"\n[{r.depth_layer}] ({r.type}) score={r.rrf_score:.4f}")
                click.echo(f"  {r.content[:200]}")
                click.echo(f"  id={r.id}")

        await client.close()

    _run(_search())


@main.command()
@click.option("--tenant", "-t", default=None, help="tenant/project scope")
def stats(tenant: str | None) -> None:
    """Show memory statistics."""

    async def _stats():
        from synapto.config import load_config
        from synapto.db.postgres import PostgresClient
        from synapto.repositories.entities import EntityRepository
        from synapto.repositories.memories import MemoryRepository

        config = load_config()
        client = PostgresClient(config.pg_dsn)
        await client.connect()

        t = tenant or config.default_tenant
        mem_repo = MemoryRepository(client)
        ent_repo = EntityRepository(client)

        by_type = await mem_repo.count_by_type(t if tenant else None)
        by_depth = await mem_repo.count_by_depth(t if tenant else None)
        total = sum(r["cnt"] for r in by_type)
        entity_count = await ent_repo.count(t if tenant else None)

        click.echo(f"total memories: {total}")
        click.echo(f"total entities: {entity_count}")
        click.echo("\nby type:")
        for r in by_type:
            click.echo(f"  {r['type']}: {r['cnt']}")
        click.echo("\nby depth layer:")
        for r in by_depth:
            click.echo(f"  {r['depth_layer']}: {r['cnt']}")

        await client.close()

    _run(_stats())


@main.command()
def doctor() -> None:
    """Check system health — PostgreSQL, Redis, embeddings, config, and schema."""

    def _green(msg: str) -> str:
        return click.style(f"[ok]   {msg}", fg="green")

    def _warn(msg: str, fix: str) -> str:
        return click.style(f"[warn] {msg}", fg="yellow") + f"\n       fix: {fix}"

    def _fail(msg: str, fix: str) -> str:
        return click.style(f"[fail] {msg}", fg="red") + f"\n       fix: {fix}"

    from synapto.config import CONFIG_FILE, load_config

    click.echo("synapto doctor\n")

    # 1. config file
    if CONFIG_FILE.exists():
        click.echo(_green(f"config file exists: {CONFIG_FILE}"))
    else:
        click.echo(_warn(f"config file missing: {CONFIG_FILE}", "run: synapto init"))

    config = load_config()

    # 2. postgresql connectivity
    async def _check_pg():
        from synapto.db.postgres import PostgresClient

        client = PostgresClient(config.pg_dsn, min_size=1, max_size=1)
        await client.connect()
        row = await client.execute_one("SELECT version() AS v;")
        await client.close()
        return row["v"]

    try:
        pg_version = _run(_check_pg())
        short = pg_version.split(",")[0] if pg_version else "unknown"
        click.echo(_green(f"postgresql: {short}"))
    except Exception as e:
        click.echo(_fail(f"postgresql: {e}", f"check connection to {config.pg_dsn}"))

    # 3. pgvector extension
    async def _check_pgvector():
        from synapto.db.postgres import PostgresClient

        client = PostgresClient(config.pg_dsn, min_size=1, max_size=1)
        await client.connect()
        row = await client.execute_one(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
        )
        await client.close()
        return row

    try:
        ext_row = _run(_check_pgvector())
        if ext_row:
            click.echo(_green(f"pgvector extension: v{ext_row['extversion']}"))
        else:
            click.echo(_warn("pgvector extension not installed", "run: CREATE EXTENSION vector;"))
    except Exception as e:
        click.echo(_fail(f"pgvector check: {e}", "ensure postgresql is reachable"))

    # 4. redis connectivity
    async def _check_redis():
        import redis.asyncio as aioredis

        client = aioredis.from_url(config.redis_url, decode_responses=True)
        info = await client.info("server")
        await client.aclose()
        return info.get("redis_version", "unknown")

    try:
        redis_ver = _run(_check_redis())
        click.echo(_green(f"redis: v{redis_ver}"))
    except Exception as e:
        click.echo(_fail(f"redis: {e}", f"check connection to {config.redis_url}"))

    # 5. embedding model
    try:
        from synapto.embeddings.registry import get_provider

        provider = get_provider(config.embedding_provider)
        click.echo(_green(f"embedding model: {provider.name} (dim={provider.dimension})"))
    except Exception as e:
        click.echo(_fail(f"embedding model: {e}", "check embedding provider config"))

    # 6. schema version
    async def _check_schema():
        from synapto.db.migrations import get_schema_version
        from synapto.db.postgres import PostgresClient

        client = PostgresClient(config.pg_dsn, min_size=1, max_size=1)
        await client.connect()
        version = await get_schema_version(client)
        await client.close()
        return version

    try:
        schema_v = _run(_check_schema())
        if schema_v:
            click.echo(_green(f"schema version: v{schema_v}"))
        else:
            click.echo(_warn("schema not initialized", "run: synapto init"))
    except Exception:
        click.echo(_warn("schema check skipped (postgresql unreachable)", "fix postgresql first"))

    click.echo()


@main.group()
def migrate() -> None:
    """Database migration management."""


main.add_command(migrate)


@migrate.command(name="up")
@click.option("--pg-dsn", envvar="SYNAPTO_PG_DSN", default=None)
@click.option("--to", "target", default=None, type=int, help="apply up to this version")
def migrate_up(pg_dsn: str | None, target: int | None) -> None:
    """Apply all pending migrations."""

    async def _up():
        from synapto.config import load_config
        from synapto.db.migrations import get_schema_version, migrate_up
        from synapto.db.postgres import PostgresClient

        config = load_config()
        dsn = pg_dsn or config.pg_dsn
        client = PostgresClient(dsn, min_size=1, max_size=2)
        await client.connect()
        applied = await migrate_up(client, target_version=target)
        version = await get_schema_version(client)
        await client.close()
        return applied, version

    applied, version = _run(_up())
    if applied:
        for f in applied:
            click.echo(click.style(f"  applied: {f}", fg="green"))
        click.echo(f"\nschema now at v{version}")
    else:
        click.echo(f"all migrations up to date (v{version})")


@migrate.command(name="down")
@click.option("--pg-dsn", envvar="SYNAPTO_PG_DSN", default=None)
@click.option("--to", "target", default=0, type=int, help="rollback to this version (exclusive)")
def migrate_down(pg_dsn: str | None, target: int) -> None:
    """Rollback migrations to a target version."""

    async def _down():
        from synapto.config import load_config
        from synapto.db.migrations import get_schema_version
        from synapto.db.migrations import migrate_down as _migrate_down
        from synapto.db.postgres import PostgresClient

        config = load_config()
        dsn = pg_dsn or config.pg_dsn
        client = PostgresClient(dsn, min_size=1, max_size=2)
        await client.connect()
        rolled_back = await _migrate_down(client, target_version=target)
        version = await get_schema_version(client)
        await client.close()
        return rolled_back, version

    rolled_back, version = _run(_down())
    if rolled_back:
        for f in rolled_back:
            click.echo(click.style(f"  rolled back: {f}", fg="yellow"))
        click.echo(f"\nschema now at v{version or 0}")
    else:
        click.echo("nothing to roll back")


@migrate.command(name="status")
@click.option("--pg-dsn", envvar="SYNAPTO_PG_DSN", default=None)
def migrate_status(pg_dsn: str | None) -> None:
    """Show migration status."""

    async def _status():
        from synapto.config import load_config
        from synapto.db.migrations import get_migration_status
        from synapto.db.postgres import PostgresClient

        config = load_config()
        dsn = pg_dsn or config.pg_dsn
        client = PostgresClient(dsn, min_size=1, max_size=2)
        await client.connect()
        status = await get_migration_status(client)
        await client.close()
        return status

    statuses = _run(_status())
    if not statuses:
        click.echo("no migrations found")
        return

    for s in statuses:
        if s["status"] == "applied":
            checksum = " (checksum mismatch!)" if s["checksum_ok"] is False else ""
            click.echo(click.style(f"  [applied] {s['filename']}{checksum}", fg="green"))
        else:
            click.echo(click.style(f"  [pending] {s['filename']}", fg="yellow"))


@main.command(name="export")
@click.option("--tenant", "-t", default=None, help="tenant/project scope")
@click.option("--output", "-o", default="-", help="output file (- for stdout)")
def export_cmd(tenant: str | None, output: str) -> None:
    """Export memories as JSON."""

    async def _export():
        from synapto.config import load_config
        from synapto.db.postgres import PostgresClient

        config = load_config()
        client = PostgresClient(config.pg_dsn)
        await client.connect()

        t = tenant or config.default_tenant
        rows = await client.execute(
            """
            SELECT id, content, summary, type, tenant, depth_layer, metadata, created_at, accessed_at
            FROM memories WHERE deleted_at IS NULL AND tenant = %s ORDER BY created_at;
            """,
            (t,),
        )

        data = [
            {k: str(v) if k in ("id", "created_at", "accessed_at") else v for k, v in row.items()}
            for row in rows
        ]

        text = json.dumps(data, indent=2, ensure_ascii=False)

        if output == "-":
            click.echo(text)
        else:
            with open(output, "w") as f:
                f.write(text)
            click.echo(f"exported {len(data)} memories to {output}")

        await client.close()

    _run(_export())


@main.command(name="import")
@click.argument("file_path")
@click.option("--tenant", "-t", default=None, help="tenant/project scope")
@click.option("--format", "fmt", type=click.Choice(["json", "markdown"]), default="json")
def import_cmd(file_path: str, tenant: str | None, fmt: str) -> None:
    """Import memories from JSON or MEMORY.md file."""

    async def _import():
        from psycopg.types.json import Jsonb

        from synapto.config import load_config
        from synapto.db.migrations import ensure_hnsw_index, run_migrations
        from synapto.db.postgres import PostgresClient
        from synapto.embeddings.registry import get_provider

        config = load_config()
        t = tenant or config.default_tenant

        client = PostgresClient(config.pg_dsn)
        await client.connect()
        await run_migrations(client)

        provider = get_provider(config.embedding_provider)
        await ensure_hnsw_index(client, provider.dimension)

        with open(file_path) as f:
            raw = f.read()

        if fmt == "json":
            data = json.loads(raw)
        else:
            # parse MEMORY.md — each section becomes a memory
            data = _parse_markdown_memories(raw, t)

        count = 0
        for item in data:
            content = item.get("content", "")
            if not content.strip():
                continue
            emb = await provider.embed_one(content)
            await client.execute(
                """
                INSERT INTO memories (content, summary, embedding, embedding_dim, type, tenant, depth_layer, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    content,
                    item.get("summary"),
                    emb,
                    provider.dimension,
                    item.get("type", "general"),
                    t,
                    item.get("depth_layer", "stable"),
                    Jsonb(item.get("metadata", {})),
                ),
            )
            count += 1

        click.echo(f"imported {count} memories into tenant '{t}'")
        await client.close()

    _run(_import())


def _detect_mcp_clients(home=None) -> list[dict]:
    """Detect installed MCP clients and their config paths."""
    from pathlib import Path

    home = home or Path.home()
    clients = []

    # claude code
    for path in [home / ".claude" / ".mcp.json", home / ".claude" / "settings.json"]:
        if path.parent.exists():
            clients.append({"name": "Claude Code", "path": path, "key": "mcpServers"})
            break

    # cursor
    cursor_path = home / ".cursor" / "mcp.json"
    if cursor_path.parent.exists():
        clients.append({"name": "Cursor", "path": cursor_path, "key": "mcpServers"})

    return clients


def _write_mcp_config(config_path, tenant: str = "default") -> None:
    """Write synapto MCP config using uvx for auto-updates."""
    from pathlib import Path

    path = Path(config_path)
    existing = {}
    if path.exists():
        with open(path) as f:
            existing = json.loads(f.read())

    servers = existing.get("mcpServers", {})
    server_config: dict = {
        "command": "uvx",
        "args": ["synapto", "serve"],
    }
    if tenant != "default":
        server_config["env"] = {"SYNAPTO_DEFAULT_TENANT": tenant}

    servers["synapto"] = server_config
    existing["mcpServers"] = servers

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(existing, indent=2))


def _offer_mcp_config(tenant: str = "default") -> None:
    """Detect MCP clients and offer to write uvx-based config."""
    clients = _detect_mcp_clients()
    if not clients:
        return

    click.echo("\n--- mcp client configuration ---")
    for client in clients:
        if click.confirm(f"configure {client['name']} with auto-update (uvx)?", default=True):
            _write_mcp_config(client["path"], tenant)
            click.echo(f"  written: {client['path']}")
        else:
            click.echo(f"  skipped: {client['name']}")


def _offer_memory_migration() -> None:
    """Scan for existing AI agent memories and offer to preview the results."""
    from synapto.migration.detect import detect_all

    click.echo("\n--- memory migration ---")
    click.echo("scanning for existing memories...")

    result = detect_all()
    if not result.sources:
        click.echo("no existing memories found")
        return

    by_client = result.by_client()
    for client_name, sources in by_client.items():
        click.echo(f"\n  {client_name}:")
        for src in sources[:10]:
            label = f"({src.format})"
            if src.estimated_count > 1:
                label += f" ~{src.estimated_count} entries"
            click.echo(f"    {src.path} {label}")
        if len(sources) > 10:
            click.echo(f"    ... and {len(sources) - 10} more")

    click.echo(f"\nfound {len(result.sources)} sources (~{result.total_estimated} estimated memories)")
    click.echo("run 'synapto migrate-memories' to import them.")


@main.command(name="migrate-memories")
@click.option("--dry-run", is_flag=True, help="preview without importing")
@click.option("--home", default=None, type=click.Path(exists=True), help="override home directory for scanning")
def migrate_memories(dry_run: bool, home: str | None) -> None:
    """Detect and import existing AI agent memories."""
    from pathlib import Path

    from synapto.migration.detect import detect_all

    scan_home = Path(home) if home else None
    result = detect_all(scan_home)

    if not result.sources:
        click.echo("no existing memories found")
        return

    by_client = result.by_client()
    for client_name, sources in by_client.items():
        click.echo(f"\n  {client_name}:")
        for src in sources:
            label = f"({src.format})"
            if src.estimated_count > 1:
                label += f" ~{src.estimated_count} entries"
            click.echo(f"    {src.path} {label}")

    click.echo(f"\nfound {len(result.sources)} sources (~{result.total_estimated} estimated memories)")

    if dry_run:
        # parse and show what would be imported
        all_parsed = _parse_all_sources(result.sources)
        click.echo(f"\nwould import {len(all_parsed)} memories:")
        by_type: dict[str, int] = {}
        by_depth: dict[str, int] = {}
        for mem in all_parsed:
            by_type[mem.memory_type] = by_type.get(mem.memory_type, 0) + 1
            by_depth[mem.depth_layer] = by_depth.get(mem.depth_layer, 0) + 1
        for t, c in sorted(by_type.items()):
            click.echo(f"  {t}: {c}")
        click.echo("by depth layer:")
        for d, c in sorted(by_depth.items()):
            click.echo(f"  {d}: {c}")
        return

    if not click.confirm("migrate these memories to synapto?", default=True):
        click.echo("migration cancelled")
        return

    all_parsed = _parse_all_sources(result.sources)
    if not all_parsed:
        click.echo("no memories could be parsed from the detected sources")
        return

    async def _do_import():
        from datetime import datetime, timezone

        from psycopg.types.json import Jsonb

        from synapto.config import load_config
        from synapto.db.migrations import ensure_hnsw_index, run_migrations
        from synapto.db.postgres import PostgresClient
        from synapto.embeddings.registry import get_provider
        from synapto.repositories.memories import MemoryRepository

        config = load_config()
        client = PostgresClient(config.pg_dsn)
        await client.connect()
        await run_migrations(client)

        provider = get_provider(config.embedding_provider)
        await ensure_hnsw_index(client, provider.dimension)

        mem_repo = MemoryRepository(client)

        # Skip memories whose source file is already in the DB so retries after
        # a partial run (Ctrl-C, timeout, OOM) don't create duplicates.
        existing = await mem_repo.find_existing_original_files(config.default_tenant)
        pending = [
            m for m in all_parsed
            if m.metadata.get("original_file") not in existing
        ]
        already = len(all_parsed) - len(pending)
        if already:
            click.echo(f"  skipping {already} memories already imported")

        if not pending:
            await client.close()
            return 0

        migrated_at = datetime.now(timezone.utc).isoformat()
        count = 0

        # Embed in batches — one provider round-trip per N texts instead of per memory.
        for batch_start in range(0, len(pending), EMBEDDING_BATCH_SIZE):
            batch = pending[batch_start:batch_start + EMBEDDING_BATCH_SIZE]
            embeddings = await provider.embed([m.content for m in batch])

            for mem, embedding in zip(batch, embeddings):
                meta = {**mem.metadata, "migrated_at": migrated_at}
                try:
                    async with client.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute(
                                """
                                INSERT INTO memories
                                    (content, summary, embedding, embedding_dim,
                                     type, tenant, depth_layer, metadata)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                                """,
                                (
                                    mem.content,
                                    mem.summary,
                                    embedding,
                                    provider.dimension,
                                    mem.memory_type,
                                    config.default_tenant,
                                    mem.depth_layer,
                                    Jsonb(meta),
                                ),
                            )
                except Exception as exc:
                    logger.warning(
                        "failed to import %s: %s",
                        mem.metadata.get("original_file", "?"),
                        exc,
                    )
                    continue

                count += 1
                if count % 10 == 0:
                    click.echo(f"  [{count}/{len(pending)}] importing...")

        await client.close()
        return count

    imported = _run(_do_import())
    click.echo(f"\nmigrated {imported} memories")


def _parse_all_sources(sources) -> list:
    """Parse all detected sources into ParsedMemory objects."""
    from synapto.migration.parse import parse_memory_file, parse_memory_index, parse_transcript

    all_parsed = []
    seen_files: set[str] = set()

    # process indexes first to track which files they cover
    for src in sources:
        if src.format == "memory-index":
            parsed = parse_memory_index(src.path)
            all_parsed.extend(parsed)
            # mark linked files as seen
            for p in parsed:
                original = p.metadata.get("original_file", "")
                if original:
                    seen_files.add(original)

    # then individual files, skipping those already covered by an index
    for src in sources:
        if src.format == "memory-file" and str(src.path) not in seen_files:
            all_parsed.extend(parse_memory_file(src.path))
        elif src.format == "transcript":
            all_parsed.extend(parse_transcript(src.path))

    return all_parsed


def _parse_markdown_memories(text: str, tenant: str) -> list[dict]:
    """Parse a MEMORY.md-style file into memory entries."""
    memories = []
    current_section = None
    current_content = []

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_section and current_content:
                memories.append({
                    "content": "\n".join(current_content).strip(),
                    "summary": current_section,
                    "type": "reference",
                    "depth_layer": "stable",
                    "metadata": {"source": "MEMORY.md"},
                })
            current_section = line[3:].strip()
            current_content = []
        elif current_section:
            current_content.append(line)

    if current_section and current_content:
        memories.append({
            "content": "\n".join(current_content).strip(),
            "summary": current_section,
            "type": "reference",
            "depth_layer": "stable",
            "metadata": {"source": "MEMORY.md"},
        })

    return memories


if __name__ == "__main__":
    main()
