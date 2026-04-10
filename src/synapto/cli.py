"""Synapto CLI — command-line interface for managing the memory graph."""

from __future__ import annotations

import asyncio
import json
import logging

import click

from synapto import __version__


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

        config = load_config()
        client = PostgresClient(config.pg_dsn)
        await client.connect()

        t = tenant or config.default_tenant
        tenant_filter = "WHERE deleted_at IS NULL AND tenant = %s" if tenant else "WHERE deleted_at IS NULL"
        params = (t,) if tenant else ()

        by_type = await client.execute(
            f"SELECT type, count(*) as cnt FROM memories {tenant_filter} GROUP BY type;", params
        )
        by_depth = await client.execute(
            f"SELECT depth_layer, count(*) as cnt FROM memories {tenant_filter} GROUP BY depth_layer;", params
        )
        total = sum(r["cnt"] for r in by_type)
        entity_count = await client.execute_one(
            "SELECT count(*) as cnt FROM entities" + (" WHERE tenant = %s" if tenant else ""),
            (t,) if tenant else None,
        )

        click.echo(f"total memories: {total}")
        click.echo(f"total entities: {entity_count['cnt']}")
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
