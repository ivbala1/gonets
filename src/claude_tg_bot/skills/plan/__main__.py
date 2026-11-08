"""Запрос карточки плана.

    .venv/bin/python -m claude_tg_bot.skills.plan "<план: что, где, чем проверю>"

Хук подтверждения узнаёт эту команду и показывает Ивану карточку «📋 План».
Сама команда ничего не разрешает: разрешение выдаёт бот по нажатию «✅ Делай
план», и только запуску, который её вызвал (approval, runs.py). Сюда команда
доходит уже после нажатия и лишь сообщает агенту итог.
"""
from __future__ import annotations

import sys

from ...i18n import _


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].strip():
        print(_("Нужен один аргумент: текст плана."), file=sys.stderr)
        return 2
    print(_("План одобрен: выполняй его в этом запуске. Карточкой остаётся только отправка "
            "людям."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
