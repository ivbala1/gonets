"""Заявка на перезапуск бота из рана агента (см. restart.py).

    .venv/bin/python -m claude_tg_bot.restart_request --reason "что поменялось"

Сразу отвечает: заявка принята или почему нет. Сам перезапуск бот делает
позже, когда закончится вся текущая работа, в том числе ран, который подал
заявку. Код к этому моменту должен быть закоммичен.
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

from .config import load_config
from .i18n import _
from .restart import code_problem, git_state

# Ключ темы, из рана которой подана заявка: бот кладёт его в окружение рана.
CHAT_KEY_ENV = "CLAUDE_TG_CHAT_KEY"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_tg_bot.restart_request")
    parser.add_argument("--reason", required=True, help="что поменялось, одной строкой")
    args = parser.parse_args(argv)

    head, _unused = git_state()
    problem = code_problem(head)
    if problem:
        print(_("заявка не подана: {problem}. Сначала закоммить.").format(problem=problem),
              file=sys.stderr)
        return 2

    cfg = load_config()
    host = cfg.approve_http_host
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{cfg.approve_http_port}/restart"
    headers = {"X-Approve-Token": cfg.approve_shared_secret} if cfg.approve_shared_secret else {}
    body = {"reason": args.reason.strip(), "commit": head,
            "chat_key": os.environ.get(CHAT_KEY_ENV, "")}
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=30)
    except httpx.HTTPError as e:
        print(_("бот не ответил: {error}").format(error=e), file=sys.stderr)
        return 3
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 300:
        print(_("заявка отклонена ({status}): {error}").format(
            status=r.status_code, error=data.get('error') or r.text[:200]), file=sys.stderr)
        return 2
    print(_("заявка {id} принята: бот перезапустится, когда закончится текущая "
            "работа, включая этот ран, и сообщит версию в тему. Заканчивай ответ.").format(
              id=data.get('id')))
    return 0


if __name__ == "__main__":
    sys.exit(main())
