"""Словарь per-chat настроек, переживающий рестарт бота.

Override модели (`/model`) жил только в bot_data и после каждого рестарта
молча возвращался к дефолту: 14.09.2026 диалог тренера так потерял Fable 5.1
после перезапуска ради другой правки. Здесь обычный dict, который при каждом
изменении атомарно сбрасывается в JSON и при старте читается обратно. Сессия
Claude к нему не привязана: ключ это chat_key ветки, как у сессий.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class PersistentDict(dict):
    """dict[str, str] с записью в JSON-файл на каждое изменение.

    Только строки в значениях: файл читается людьми и правится руками при
    остановленном боте. Битый файл не роняет старт: логируется и начинается
    пустой словарь, значения потеряны ровно те, что были в нём.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            raw = {}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("overrides file %s не прочитан (%s), начинаю пустым", path, e)
            raw = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and isinstance(v, str) and v:
                    super().__setitem__(k, v)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self._flush()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._flush()

    def pop(self, key, default=None):  # type: ignore[override]
        had = key in self
        value = super().pop(key, default)
        if had:
            self._flush()
        return value
