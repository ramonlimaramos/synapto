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
def init(pg_dsn: str) -> None:
    """Initialize the database — create tables, indexes, and extensions."""
    from synapto.config import save_default_config

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

    config_path = save_default_config()
    click.echo(f"config: {config_path}")

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
