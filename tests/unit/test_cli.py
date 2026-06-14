"""Unit tests for CLI MCP client detection and config writing."""

from __future__ import annotations

import json

from click.testing import CliRunner

from synapto.cli import _detect_mcp_clients, _offer_mcp_config, _write_mcp_config


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
        assert data["mcpServers"]["synapto"]["args"] == ["synapto", "serve"]
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
