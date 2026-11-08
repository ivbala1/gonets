"""Корень бота и пути от него: одна точка вместо `Path(__file__).parents[N]`
в каждом модуле (ревизия повторов 07.11.2026, п. 5). Без зависимостей: модуль
зовут config, хук, скиллы и служба экрана до загрузки `.env`.

Путь из переменной окружения читается в момент вызова (скилл грузит `.env`
после импорта). Относительный путь считается от корня бота, а не от текущего
каталога: команда агента, запущенная из другого каталога, иначе завела бы
свой `state/` (в `.env` стоит `CLAUDE_TG_BOT_STATE_DIR=./state`)."""

from __future__ import annotations

import os
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = BOT_ROOT / ".env"
VENV_PYTHON = BOT_ROOT / ".venv" / "bin" / "python"


def env_path(name: str, default: str) -> Path:
    """Путь из переменной `name`; пустая или нет: `default`. `~` раскрывается,
    относительный путь (и умолчание) от `BOT_ROOT`, итог через resolve."""
    raw = os.environ.get(name, "").strip()
    path = Path(raw or default).expanduser()
    if not path.is_absolute():
        path = BOT_ROOT / path
    return path.resolve()


def state_dir() -> Path:
    """Каталог состояния бота (`CLAUDE_TG_BOT_STATE_DIR`, по умолчанию `state`)."""
    return env_path("CLAUDE_TG_BOT_STATE_DIR", "state")


def hook_settings() -> Path:
    """Настройки хука подтверждения для claude (`CLAUDE_HOOK_SETTINGS`)."""
    return env_path("CLAUDE_HOOK_SETTINGS", "claude-hook-settings.json")
