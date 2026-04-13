"""Unit tests for CLI MCP client detection and config writing."""

from __future__ import annotations

import json

from synapto.cli import _detect_mcp_clients, _write_mcp_config


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
