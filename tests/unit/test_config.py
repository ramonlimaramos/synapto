"""Unit tests for Synapto configuration loading."""

from __future__ import annotations

import tomllib

from synapto.config import SynaptoConfig, embedding_provider_kwargs, load_config, save_default_config


def test_load_config_reads_embedding_device_from_env(monkeypatch):
    monkeypatch.setenv("SYNAPTO_EMBEDDING_DEVICE", "cpu")

    config = load_config()

    assert config.embedding_device == "cpu"


def test_load_config_reads_embedding_device_from_toml(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("""
[embeddings]
provider = "sentence-transformers"
model = "custom-model"
device = "cpu"
""")

    config = load_config(config_path)

    assert config.embedding_device == "cpu"


def test_embedding_device_env_overrides_toml(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text("""
[embeddings]
device = "mps"
""")
    monkeypatch.setenv("SYNAPTO_EMBEDDING_DEVICE", "cpu")

    config = load_config(config_path)

    assert config.embedding_device == "cpu"


def test_embedding_provider_kwargs_includes_configured_model_and_device():
    config = SynaptoConfig(
        embedding_model="custom-model",
        embedding_device="cpu",
    )

    assert embedding_provider_kwargs(config) == {
        "model_name": "custom-model",
        "device": "cpu",
    }


def test_embedding_provider_kwargs_omits_unset_values():
    assert embedding_provider_kwargs(SynaptoConfig()) == {}


def test_save_default_config_writes_embedding_device(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr("synapto.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("synapto.config.CONFIG_FILE", config_file)

    save_default_config()

    data = tomllib.loads(config_file.read_text())
    assert data["embeddings"]["device"] == ""
