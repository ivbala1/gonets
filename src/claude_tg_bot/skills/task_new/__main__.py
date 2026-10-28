"""Завести тему задачи по поручению Ивана (вход Домового, ревью Codex 15.10).

Иван говорит Домовому «обнови Immich», «разберись с X»: Домовой не делает
это в своём чате, а заводит задачу. Бот создаёт тему в группе задач и сразу
начинает обдумывание; итог будет в теме, а после её закрытия бот ответит на
сообщение Ивана одной строкой итога.

    python -m claude_tg_bot.skills.task_new "исходные слова Ивана" [--context "пояснение"]
    python -m claude_tg_bot.skills.task_new --open    # открытые задачи: id, имя, этап, ссылка
    python -m claude_tg_bot.skills.task_new "слова Ивана" --to <id> [--context "..."]

--to добавляет слова в открытую задачу (решение Ивана 19.10.2026): они видны
в её теме и идут ходом обдумывания, как сообщение Ивана там. «Делай» и
закрытие так не делаются, они только в теме.

Перед новой задачей Домовой смотрит открытые: продолжение открытой идёт в её
тему, а не новой задачей (разбор 19.10.2026: новая тема на продолжение
получила три слова, и агент 18 минут искал вводные по старым перепискам).

Слова передаются исходные, а не пересказ: по ним агент задачи поймёт, что
просили. Пояснение Домового (что было до этого в разговоре) идёт справкой.
Скилл стучится в POST /task локального сервера бота и печатает ответ: ссылку
на тему или причину отказа. Повтор той же команды в ту же минуту второй темы
не заведёт (ключ из слов и минуты).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

import httpx

from ... import tasks
from ...config import load_config
from ...i18n import _


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.skills.task_new")
    parser.add_argument("words", nargs="*", help="исходные слова Ивана")
    parser.add_argument("--context", default="", help="пояснение Домового, справка")
    parser.add_argument("--open", action="store_true", help="открытые задачи со ссылками, ничего не заводит")
    parser.add_argument("--to", default="", help="id открытой задачи: добавить слова в неё")
    args = parser.parse_args(argv)
    if args.open:
        cfg = load_config(skip_hook_validation=True)
        opened = tasks.open_tasks(tasks.load(tasks.registry_dir(cfg.state_dir)))
        print(tasks.open_list(opened, with_id=True) if opened else _("открытых задач нет"))
        return 0
    words = " ".join(args.words).strip()
    if not words:
        print("task_new: нужны слова задачи", file=sys.stderr)
        return 2
    cfg = load_config(skip_hook_validation=True)
    host = cfg.approve_http_host
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"
    key = hashlib.sha256(f"{words}\n{int(time.time() // 60)}".encode()).hexdigest()[:16]
    headers = {"X-Approve-Token": cfg.approve_shared_secret} if cfg.approve_shared_secret else {}
    try:
        # Тема рана и сообщение Ивана из окружения рана бота (claude_runner):
        # на него ответит строка итога после закрытия (19.10.2026).
        ask = os.environ.get("CLAUDE_TG_ASK_ID", "")
        r = httpx.post(f"http://{host}:{cfg.approve_http_port}/task",
                       json={"words": words, "context": args.context, "key": key, "to": args.to,
                             "chat_key": os.environ.get("CLAUDE_TG_CHAT_KEY", ""),
                             "ask_id": int(ask) if ask.isdigit() else None},
                       headers=headers, timeout=120)
    except httpx.HTTPError as e:
        print(f"task_new: бот не ответил ({type(e).__name__})", file=sys.stderr)
        return 1
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        print(f"task_new: отказ {r.status_code}: {data.get('error') or r.text[:200]}", file=sys.stderr)
        return 1
    print(data.get("text") or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
