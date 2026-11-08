"""Словарь per-chat настроек, переживающий рестарт бота.

Override модели (`/model`) жил только в bot_data и после каждого рестарта
молча возвращался к дефолту: 14.09.2026 диалог тренера так потерял Fable 5.1
после перезапуска ради другой правки. Здесь обычный dict, который при каждом
изменении атомарно сбрасывается в JSON и при старте читается обратно. Сессия
Claude к нему не привязана: ключ это chat_key ветки, как у сессий.
"""
from __future__ import annotations

from pathlib import Path

from . import jsonstore


class PersistentDict(dict):
    """dict[str, str] с записью в JSON-файл на каждое изменение.

    Только строки в значениях: файл читается людьми и правится руками при
    остановленном боте. Битый файл (не JSON, не UTF-8) не роняет старт:
    он откладывается рядом как `<имя>.corrupt-<время>`, словарь начинается
    пустым, и первая запись уже не стирает прежние значения без следа.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        for k, v in jsonstore.read_dict(path).items():
            if isinstance(k, str) and isinstance(v, str) and v:
                super().__setitem__(k, v)

    def _flush(self) -> None:
        jsonstore.write_json(self.path, dict(self), indent=2)

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
