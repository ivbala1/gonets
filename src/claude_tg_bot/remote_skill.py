"""Запуск скилла бота по ssh с другой машины: `ssh pc-skill <скилл> [аргументы]`.

Программа стоит forced command у отдельного ключа в ~/.ssh/authorized_keys:

    command="<repo>/.venv/bin/python -m claude_tg_bot.remote_skill",restrict ssh-ed25519 AAAA… MacBook -> скиллы бота

sshd не отдаёт запрошенную строку shell'у, а кладёт её в SSH_ORIGINAL_COMMAND.
Здесь она разбирается shlex'ом и запускается только
`python -m claude_tg_bot.skills.<имя>` либо модуль Домового из явного списка
DOMOVOY_SKILLS (перенос из бота 27.11.2026: имя для мака прежнее) с
аргументами списком, без shell:
`; rm`, `$(…)` и кавычки в аргументах остаются текстом аргумента. Поэтому
такой вызов можно разрешать на другой машине без вопроса, в отличие от
`ssh pc …` с полным shell, где удалённая сторона разбирает строку заново.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

from . import paths
from .i18n import _

REPO_ROOT = paths.BOT_ROOT
PYTHON = paths.VENV_PYTHON
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# PATH как у systemd-юнитов бота: claude и uv лежат в ~/.local/bin, а sshd
# отдаёт forced command системный PATH без него.
LOCAL_BIN = Path.home() / ".local" / "bin"

_SKILL_NAME = re.compile(r"[a-z][a-z0-9_]*")

# Скиллы, переехавшие в Домовой: имя для `ssh pc-skill` -> модуль Домового.
# Список явный, имя сверяется целиком; интерпретатор и корень константы, не из
# окружения. `-P`: каталог запуска не попадает в sys.path, пакет Домового
# берётся из его venv (установлен в режиме разработки).
DOMOVOY_ROOT = Path.home() / "src" / "domovoy"
DOMOVOY_PYTHON = DOMOVOY_ROOT / ".venv" / "bin" / "python"
DOMOVOY_SKILLS = {"chat_digest": "domovoy.chat_digest"}


def resolve(original: str) -> tuple[list[str], Path]:
    """(argv, cwd) для exec по строке из SSH_ORIGINAL_COMMAND; ValueError словами."""
    try:
        parts = shlex.split(original)
    except ValueError as e:
        raise ValueError(_("команда не разбирается: {error}").format(error=e)) from None
    if not parts:
        raise ValueError(_("нужно имя скилла: ssh pc-skill <скилл> [аргументы]"))
    name, args = parts[0], parts[1:]
    if name in DOMOVOY_SKILLS:
        if not DOMOVOY_PYTHON.is_file():
            raise ValueError(_("нет Домового: {path}").format(path=DOMOVOY_PYTHON))
        return [str(DOMOVOY_PYTHON), "-P", "-m", DOMOVOY_SKILLS[name], *args], DOMOVOY_ROOT
    if not _SKILL_NAME.fullmatch(name) or not (SKILLS_DIR / name / "__main__.py").is_file():
        raise ValueError(_("нет такого скилла: {name!r}").format(name=name))
    return [str(PYTHON), "-m", f"claude_tg_bot.skills.{name}", *args], REPO_ROOT


def main() -> int:
    try:
        argv, cwd = resolve(os.environ.pop("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as e:
        print(f"remote_skill: {e}", file=sys.stderr)
        return 2
    os.environ["PATH"] = f"{LOCAL_BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    # Локаль приходит с мака (AcceptEnv LANG LC_*): не-UTF-8 сломала бы вывод
    # кириллицы; Python скилла и его дочерние Python идут в режиме UTF-8.
    os.environ["PYTHONUTF8"] = "1"
    os.chdir(cwd)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    sys.exit(main())
