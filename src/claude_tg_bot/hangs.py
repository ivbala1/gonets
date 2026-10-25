"""Строка «⏳ Висит» в конце ответа по файлу срочного (HANGS_FILE).

Помощник ведёт файл срочного (строка `- ГГГГ-ММ-ДД ЧЧ:ММ | что сделать | закреп N`,
закрытое удаляется), а напоминание в каждом ответе дописывает бот: правило в
промте модель забывала (разбор недели помощника W43, предложение 1, решение
Ивана 25.10.2026). Без зависимостей.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

MARK = "⏳ Висит"
_LINE = re.compile(r"^- (\d{4}-\d\d-\d\d) (\d\d:\d\d) \| ([^|]+?)\s*(?:\|.*)?$")
# Строка модели и в виде пункта списка или жирным («- ⏳ Висит», «**⏳ Висит**»).
_MODEL_LINE = re.compile(r"^[\s>*_-]*" + re.escape(MARK) + r"\b.*$\n?", re.M)
MAX_LINES = 5


def hang_lines(path: Path, today: date) -> list[str]:
    """Строки «⏳ Висит: что (с ЧЧ:ММ)» по открытым записям файла; дата не
    сегодняшняя добавляется к часу. Больше MAX_LINES: последней строкой
    сколько ещё. Файла нет или он не читается: пусто."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        day, hm, what = m.groups()
        since = hm if day == today.isoformat() else f"{day[8:10]}.{day[5:7]} {hm}"
        out.append(f"{MARK}: {what} (с {since})")
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES] + [f"{MARK}: и ещё {len(out) - MAX_LINES}"]
    return out


def strip_model_lines(reply: str) -> str:
    """Ответ без строк «Висит», написанных моделью: при непустом файле их
    заменяют строки бота, иначе срочное шло бы дважды."""
    return _MODEL_LINE.sub("", reply).rstrip()


def with_hangs(reply: str, lines: list[str]) -> str:
    """Ответ со строками «Висит» в конце. Пустой ответ остаётся пустым:
    молчаливый ран не должен превращаться в сообщение."""
    if not lines or not reply.strip():
        return reply
    body = strip_model_lines(reply)
    return (body + "\n\n" if body else "") + "\n".join(lines)
