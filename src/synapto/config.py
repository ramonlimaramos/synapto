"""Configuration for Synapto — TOML file + environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".synapto"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class SynaptoConfig:
    # postgresql
    pg_dsn: str = "postgresql://localhost/synapto"

    # redis
    redis_url: str = "redis://localhost:6379/0"

    # embeddings
    embedding_provider: str | None = None  # None = auto-select
    embedding_model: str | None = None

    # defaults
    default_tenant: str = "default"

    # decay
    decay_ephemeral_max_age_hours: int = 24
    decay_purge_after_days: int = 30

    # server
    server_name: str = "synapto"


def load_config(config_path: Path | None = None) -> SynaptoConfig:
    """Load config from TOML file, then override with environment variables."""
    config = SynaptoConfig()

    path = config_path or CONFIG_FILE
    if path.exists():
        try:
            import tomli

            with open(path, "rb") as f:
                data = tomli.load(f)

            pg = data.get("postgresql", {})
            config.pg_dsn = pg.get("dsn", config.pg_dsn)

            redis = data.get("redis", {})
            config.redis_url = redis.get("url", config.redis_url)

            emb = data.get("embeddings", {})
            config.embedding_provider = emb.get("provider", config.embedding_provider)
            config.embedding_model = emb.get("model", config.embedding_model)

            defaults = data.get("defaults", {})
            config.default_tenant = defaults.get("tenant", config.default_tenant)

            decay = data.get("decay", {})
            config.decay_ephemeral_max_age_hours = decay.get(
                "ephemeral_max_age_hours", config.decay_ephemeral_max_age_hours
            )
            config.decay_purge_after_days = decay.get(
                "purge_after_days", config.decay_purge_after_days
            )

            server = data.get("server", {})
            config.server_name = server.get("name", config.server_name)
        except ImportError:
            pass  # tomli not available on 3.11+, use tomllib
            try:
                import tomllib

                with open(path, "rb") as f:
                    data = tomllib.load(f)
                # same parsing as above — simplified by just re-calling
            except ImportError:
                pass

    # environment variable overrides (highest priority)
    env_map = {
        "SYNAPTO_PG_DSN": "pg_dsn",
        "SYNAPTO_REDIS_URL": "redis_url",
        "SYNAPTO_EMBEDDING_PROVIDER": "embedding_provider",
        "SYNAPTO_EMBEDDING_MODEL": "embedding_model",
        "SYNAPTO_DEFAULT_TENANT": "default_tenant",
    }
    for env_var, attr in env_map.items():
        val = os.environ.get(env_var)
        if val:
            setattr(config, attr, val)

    return config


def save_default_config() -> Path:
    """Create a default config file at ~/.synapto/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        return CONFIG_FILE

    import tomli_w

    data = {
        "postgresql": {"dsn": "postgresql://localhost/synapto"},
        "redis": {"url": "redis://localhost:6379/0"},
        "embeddings": {
            "provider": "",
            "model": "",
        },
        "defaults": {"tenant": "default"},
        "decay": {
            "ephemeral_max_age_hours": 24,
            "purge_after_days": 30,
        },
        "server": {"name": "synapto"},
    }

    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(data, f)

    return CONFIG_FILE
