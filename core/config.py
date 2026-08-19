from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

CONFIG_PATH = ROOT_DIR / "config.json"
CONFIG_EXAMPLE_PATH = ROOT_DIR / "config.example.json"


class Config:
    """Обёртка над config.json с доступом по точечному пути (например "tickets.curator.role_id")."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        source = self._path if self._path.exists() else CONFIG_EXAMPLE_PATH
        with open(source, "r", encoding="utf-8") as f:
            self._data = json.load(f)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node


config = Config(CONFIG_PATH)


def log_channel_id(system: str) -> int | None:
    """ID канала логов для конкретной системы (log_channels.<system>), либо общий
    канал channels.logs, если для системы отдельный канал ещё не задан."""
    return config.get(f"log_channels.{system}") or config.get("channels.logs")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "").strip()
TEST_GUILDS = [int(GUILD_ID)] if GUILD_ID.isdigit() else None
