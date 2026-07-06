"""Layered configuration.

Precedence (lowest → highest):
    1. ``config/settings.yaml``        committed defaults
    2. ``config/settings.local.yaml``  gitignored local overrides
    3. ``.env`` / environment          secrets (prefixed ``MOI_``)

Access the singleton via :func:`get_settings`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Repository root = two levels up from this file (src/moi/config.py).
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


class IBKRSettings(BaseSettings):
    """Interactive Brokers gateway/TWS connection settings."""

    host: str = "127.0.0.1"
    port: int = 7497  # 7497 = TWS paper, 4002 = IB Gateway paper, 7496/4001 = live
    client_id: int = 17
    account: str | None = None  # optional explicit account id (e.g. "DU1234567")
    readonly: bool = True  # Phase 0: never place orders


class _YamlSource(PydanticBaseSettingsSource):
    """Settings source for the layered YAML files (committed defaults + local overrides)."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = _deep_merge(
            _load_yaml(CONFIG_DIR / "settings.yaml"),
            _load_yaml(CONFIG_DIR / "settings.local.yaml"),
        )

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="MOI_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence, highest first: real env vars > .env file > YAML files > defaults.
        return (init_settings, env_settings, dotenv_settings, _YamlSource(settings_cls))

    env: str = "paper"  # "paper" | "live"
    log_level: str = "INFO"
    log_json: bool = False

    db_path: Path = DATA_DIR / "moi.duckdb"

    ibkr: IBKRSettings = Field(default_factory=IBKRSettings)

    # SEC EDGAR requires a contact identity ("Name email") on every request.
    edgar_identity: str = "Idan Kligvasser kligvasser@gmail.com"

    # Data-source API keys (secrets — set in .env, never committed).
    quiver_api_key: str | None = None
    unusualwhales_api_key: str | None = None
    fred_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Price backfill window in years.
    price_history_years: int = 3

    # Execution limits (Phase 4). The executor refuses anything beyond these.
    max_order_usd: float = 8_000.0
    max_daily_usd: float = 30_000.0
    # The executor only trades paper accounts (DU...) unless this is explicitly true.
    allow_live: bool = False


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a mapping at the top level.")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build and cache the settings singleton (YAML files + .env + environment)."""
    return Settings()
