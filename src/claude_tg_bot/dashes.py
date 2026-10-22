"""Длинное и среднее тире на дефис в тексте, который бот шлёт Ивану.

Хук против тире (svod agent/hooks/no_dash_guard.py) в ранах бота молчит: бот
шлёт в чат только последний текст рана, и строка хука «Исправлено» подменяла
сам ответ (решение Ивана 22.10.2026). Без зависимостей: модуль берут и бот, и
разовые запуски скиллов.
"""
from __future__ import annotations

import re

_DASH_TO_HYPHEN = str.maketrans({"\u2014": "-", "\u2013": "-"})
_DASH_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# Строчный код любой длины забора (`x`, ``x``), цель ссылки Markdown и голые
# адреса: тире в них не проза.
# Забор строчного кода закрывается ровно той же длиной (не частью более
# длинного), адрес не забирает закрывающую кавычку цитаты.
_DASH_KEEP = re.compile(
    r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)|\]\([^)\n]*\)|https?://[^\s«»“”]+")
# Цитаты: от открывающей до парной закрывающей в той же строке, вложенные
# внутри внешней целиком; незакрытая кавычка остаток строки не прячет, а
# закрытые пары после неё защищены.
_DASH_QUOTES = (("«", "»"), ("“", "”"))


def _keep_spans(line: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _DASH_KEEP.finditer(line)]
    for open_, close in _DASH_QUOTES:
        stack: list[int] = []
        for i, ch in enumerate(line):
            if any(a <= i < b for a, b in spans):
                continue
            if ch == open_:
                stack.append(i)
            elif ch == close and stack:
                spans.append((stack.pop(), i + 1))
    return sorted(spans)


def _soften_line(line: str) -> str:
    if line.lstrip().startswith(">"):
        return line  # цитата Telegram: чужой текст дословно
    out, pos = [], 0
    for a, b in _keep_spans(line):
        if a < pos:
            a = pos
        if a >= b:
            continue
        out.append(line[pos:a].translate(_DASH_TO_HYPHEN))
        out.append(line[a:b])
        pos = b
    out.append(line[pos:].translate(_DASH_TO_HYPHEN))
    return "".join(out)


def soften_dashes(text: str) -> str:
    """Длинное и среднее тире в прозе ответа на дефис. Хук против тире в ранах
    бота молчит: его строка «Исправлено» подменяла сам ответ (решение Ивана
    22.10.2026). Как у хука (svod agent/hooks/no_dash_guard.py): блоки кода
    ``` и ~~~, `строчный` код и цитаты не трогаются, точные данные и чужой
    текст ради стиля не искажаются."""
    if "—" not in text and "–" not in text:
        return text
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        if fence is None:
            m = _DASH_FENCE_OPEN.match(line)
            # У забора из обратных кавычек их не бывает в строке информации.
            if m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):
                fence = m.group(1)
                out.append(line)
            else:
                out.append(_soften_line(line))
        else:
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + r"{%d,}[ \t]*$" % len(fence),
                        line.rstrip("\n")):
                fence = None
            out.append(line)
    return "".join(out)
