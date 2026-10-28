"""Сверка текстов бота без привязки к языку прогона.

Ожидаемый текст берётся той же функцией перевода, что и в коде: msgid
пишется целиком, как в исходнике, и проходит через `_()`. По-русски сверка
идёт с исходником, при CLAUDE_TG_BOT_TEST_LANG=en с каталогом; строка без
перевода краснеет в английском прогоне.
"""
from __future__ import annotations

import re
import string

from claude_tg_bot.i18n import _


def msg_re(msgid: str) -> str:
    """Регулярка для assertRegex: перевод msgid, подстановки {name} любым текстом."""
    out = []
    for literal, field, _spec, _conv in string.Formatter().parse(_(msgid)):
        out.append(re.escape(literal))
        if field is not None:
            out.append(".*?")
    return "(?s)" + "".join(out)
