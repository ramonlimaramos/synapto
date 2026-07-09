"""Unit tests for CLI MCP client detection and config writing."""

from __future__ import annotations

import json

from click.testing import CliRunner

from synapto.cli import _detect_mcp_clients, _offer_mcp_config, _write_mcp_config, main


class TestDetectMcpClients:
    def test_detects_claude_code(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        clients = _detect_mcp_clients(home=tmp_path)
        names = [c["name"] for c in clients]
        assert "Claude Code" in names

    def test_detects_cursor(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()

        clients = _detect_mcp_clients(home=tmp_path)
        names = [c["name"] for c in clients]
        assert "Cursor" in names

    def test_detects_both(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".cursor").mkdir()

        clients = _detect_mcp_clients(home=tmp_path)
        assert len(clients) == 2

    def test_detects_none(self, tmp_path):
        clients = _detect_mcp_clients(home=tmp_path)
        assert len(clients) == 0


class TestWriteMcpConfig:
    def test_creates_new_config(self, tmp_path):
        config_path = tmp_path / "mcp.json"

        _write_mcp_config(config_path, tenant="default")

        data = json.loads(config_path.read_text())
        assert data["mcpServers"]["synapto"]["command"] == "uvx"
        assert data["mcpServers"]["synapto"]["args"] == ["--refresh", "synapto", "serve"]
        assert "env" not in data["mcpServers"]["synapto"]

    def test_creates_config_with_custom_tenant(self, tmp_path):
        config_path = tmp_path / "mcp.json"

        _write_mcp_config(config_path, tenant="my-project")

        data = json.loads(config_path.read_text())
        assert data["mcpServers"]["synapto"]["env"]["SYNAPTO_DEFAULT_TENANT"] == "my-project"

    def test_can_disable_claude_code_auto_memory(self, tmp_path):
        config_path = tmp_path / "mcp.json"

        _write_mcp_config(
            config_path,
            tenant="my-project",
            disable_claude_auto_memory=True,
        )

        data = json.loads(config_path.read_text())
        env = data["mcpServers"]["synapto"]["env"]
        assert env["SYNAPTO_DEFAULT_TENANT"] == "my-project"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    def test_preserves_existing_servers(self, tmp_path):
        config_path = tmp_path / "mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "other-server": {"command": "other", "args": ["serve"]}
            }
        }))

        _write_mcp_config(config_path, tenant="default")

        data = json.loads(config_path.read_text())
        assert "other-server" in data["mcpServers"]
        assert "synapto" in data["mcpServers"]

    def test_overwrites_existing_synapto_config(self, tmp_path):
        config_path = tmp_path / "mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "synapto": {"command": "/old/path/synapto", "args": ["serve"]}
            }
        }))

        _write_mcp_config(config_path, tenant="default")

        data = json.loads(config_path.read_text())
        assert data["mcpServers"]["synapto"]["command"] == "uvx"
        assert data["mcpServers"]["synapto"]["args"] == ["--refresh", "synapto", "serve"]

    def test_preserves_existing_synapto_command_when_upgrading(self, tmp_path):
        config_path = tmp_path / "mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "synapto": {
                    "command": "uv",
                    "args": ["--directory", "/repo/synapto", "run", "synapto", "serve"],
                    "env": {"SYNAPTO_DEFAULT_TENANT": "existing"},
                }
            }
        }))

        _write_mcp_config(
            config_path,
            tenant=None,
            disable_claude_auto_memory=True,
            preserve_existing_synapto=True,
        )

        data = json.loads(config_path.read_text())
        server = data["mcpServers"]["synapto"]
        assert server["command"] == "uv"
        assert server["args"] == ["--directory", "/repo/synapto", "run", "synapto", "serve"]
        assert server["env"]["SYNAPTO_DEFAULT_TENANT"] == "existing"
        assert server["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    def test_cursor_upgrade_removes_claude_only_env(self, tmp_path):
        config_path = tmp_path / "mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "synapto": {
                    "command": "uvx",
                    "args": ["--refresh", "synapto", "serve"],
                    "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                }
            }
        }))

        _write_mcp_config(
            config_path,
            tenant=None,
            disable_claude_auto_memory=False,
            preserve_existing_synapto=True,
        )

        data = json.loads(config_path.read_text())
        assert "env" not in data["mcpServers"]["synapto"]

    def test_creates_parent_directories(self, tmp_path):
        config_path = tmp_path / "nested" / "dir" / "mcp.json"

        _write_mcp_config(config_path, tenant="default")

        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert data["mcpServers"]["synapto"]["command"] == "uvx"


class TestOfferMcpConfig:
    def test_disables_claude_code_auto_memory_only_for_claude(self, tmp_path, monkeypatch):
        claude_config = tmp_path / "claude.json"
        cursor_config = tmp_path / "cursor.json"

        import synapto.cli as cli

        monkeypatch.setattr(
            cli,
            "_detect_mcp_clients",
            lambda: [
                {"name": "Claude Code", "path": claude_config, "key": "mcpServers"},
                {"name": "Cursor", "path": cursor_config, "key": "mcpServers"},
            ],
        )
        monkeypatch.setattr(cli.click, "confirm", lambda *args, **kwargs: True)

        _offer_mcp_config(tenant="default")

        claude_data = json.loads(claude_config.read_text())
        cursor_data = json.loads(cursor_config.read_text())
        assert (
            claude_data["mcpServers"]["synapto"]["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"]
            == "1"
        )
        assert "env" not in cursor_data["mcpServers"]["synapto"]


class TestConfigureMcpCommand:
    def test_configure_mcp_upgrades_detected_claude_config(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config_path = claude_dir / ".mcp.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "synapto": {
                    "command": "uv",
                    "args": ["--directory", "/repo/synapto", "run", "synapto", "serve"],
                }
            }
        }))

        result = CliRunner().invoke(
            main,
            ["configure-mcp", "--home", str(tmp_path), "--client", "claude-code", "--tenant", "project-a", "--yes"],
        )

        assert result.exit_code == 0
        data = json.loads(config_path.read_text())
        server = data["mcpServers"]["synapto"]
        assert server["command"] == "uv"
        assert server["args"] == ["--directory", "/repo/synapto", "run", "synapto", "serve"]
        assert server["env"]["SYNAPTO_DEFAULT_TENANT"] == "project-a"
        assert server["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert "restart your MCP client" in result.output

    def test_configure_mcp_updates_cursor_without_claude_env(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        config_path = cursor_dir / "mcp.json"

        result = CliRunner().invoke(
            main,
            ["configure-mcp", "--home", str(tmp_path), "--client", "cursor", "--tenant", "project-a", "--yes"],
        )

        assert result.exit_code == 0
        data = json.loads(config_path.read_text())
        server = data["mcpServers"]["synapto"]
        assert server["env"] == {"SYNAPTO_DEFAULT_TENANT": "project-a"}


class TestServeCommand:
    def test_serve_disables_fastmcp_banner(self, monkeypatch):
        """FastMCP's Rich banner bypasses logging, so serve must suppress it."""
        from synapto import server
        from synapto.cli import main

        calls = []

        class DummyMCP:
            def run(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(server, "mcp", DummyMCP())

        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 0
        assert calls == [{"show_banner": False}]


class _FakeDbClient:
    """Minimal PostgresClient stand-in capturing SQL calls."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.rows


class _FakeProvider:
    dimension = 3

    async def embed_one(self, text):
        return [0.0, 0.0, 0.0]


def _patch_cli_db(monkeypatch, fake_client):
    from types import SimpleNamespace

    config = SimpleNamespace(pg_dsn="postgresql://fake", default_tenant="cli_test", embedding_provider="fake")
    monkeypatch.setattr("synapto.config.load_config", lambda: config)
    monkeypatch.setattr("synapto.config.embedding_provider_kwargs", lambda cfg: {})
    monkeypatch.setattr("synapto.db.postgres.PostgresClient", lambda dsn: fake_client)
    monkeypatch.setattr("synapto.embeddings.registry.get_provider", lambda name, **kw: _FakeProvider())

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("synapto.db.migrations.ensure_hnsw_index", _noop)
    monkeypatch.setattr("synapto.db.migrations.run_migrations", _noop)


class TestDomainCliPlumbing:
    def test_search_forwards_domain_filter(self, monkeypatch):
        fake_client = _FakeDbClient()
        _patch_cli_db(monkeypatch, fake_client)
        captured = {}

        async def fake_hybrid_search(client, provider, query, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("synapto.search.hybrid.hybrid_search", fake_hybrid_search)

        result = CliRunner().invoke(main, ["search", "timeouts", "--domain", "python"])

        assert result.exit_code == 0, result.output
        assert captured["domain"] == "python"

    def test_export_includes_domain_key(self, monkeypatch):
        fake_client = _FakeDbClient(
            rows=[
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "content": "fact",
                    "summary": None,
                    "type": "project",
                    "subtype": None,
                    "domain": "python",
                    "tenant": "cli_test",
                    "depth_layer": "stable",
                    "metadata": {},
                    "created_at": "2026-07-09",
                    "accessed_at": "2026-07-09",
                }
            ]
        )
        _patch_cli_db(monkeypatch, fake_client)

        result = CliRunner().invoke(main, ["export"])

        assert result.exit_code == 0, result.output
        select_sql = fake_client.calls[0][0]
        assert "domain" in select_sql
        assert json.loads(result.output)[0]["domain"] == "python"

    def test_import_persists_domain_from_json(self, monkeypatch, tmp_path):
        fake_client = _FakeDbClient()
        _patch_cli_db(monkeypatch, fake_client)

        payload = [{"content": "fact", "domain": "python", "subtype": "workflow"}]
        source = tmp_path / "memories.json"
        source.write_text(json.dumps(payload))

        result = CliRunner().invoke(main, ["import", str(source)])

        assert result.exit_code == 0, result.output
        insert_sql, insert_params = fake_client.calls[0]
        assert "domain" in insert_sql
        # positional asserts guard against column/parameter misalignment in the 10-column INSERT
        assert insert_params[5] == "workflow"
        assert insert_params[6] == "python"

    def test_import_defaults_domain_to_none_for_legacy_payloads(self, monkeypatch, tmp_path):
        fake_client = _FakeDbClient()
        _patch_cli_db(monkeypatch, fake_client)

        source = tmp_path / "legacy.json"
        source.write_text(json.dumps([{"content": "pre-domain memory"}]))

        result = CliRunner().invoke(main, ["import", str(source)])

        assert result.exit_code == 0, result.output
        _, insert_params = fake_client.calls[0]
        assert insert_params[6] is None

    def test_export_import_round_trip_preserves_domain(self, monkeypatch, tmp_path):
        base_row = {
            "content": "fact",
            "summary": None,
            "type": "project",
            "subtype": None,
            "tenant": "cli_test",
            "depth_layer": "stable",
            "metadata": {},
            "created_at": "2026-07-09",
            "accessed_at": "2026-07-09",
        }
        export_client = _FakeDbClient(
            rows=[
                {"id": "00000000-0000-0000-0000-000000000001", "domain": "python", **base_row},
                {"id": "00000000-0000-0000-0000-000000000002", "domain": None, **base_row},
            ]
        )
        _patch_cli_db(monkeypatch, export_client)
        exported = CliRunner().invoke(main, ["export"])
        assert exported.exit_code == 0, exported.output

        source = tmp_path / "round_trip.json"
        source.write_text(exported.output)
        import_client = _FakeDbClient()
        _patch_cli_db(monkeypatch, import_client)

        result = CliRunner().invoke(main, ["import", str(source)])

        assert result.exit_code == 0, result.output
        assert [params[6] for _, params in import_client.calls] == ["python", None]
