"""Правила хранения каталога state/ бота и отчёт суточной уборки (этап 1
уборки временного, решение Ивана 09.11.2026).

У каждого вида данных есть правило: срок, после которого уборка бота
(`bot._attachment_cleanup_loop`) его удаляет, или пометка, почему оно
хранится («постоянно», «справочник», «этап 2»: срок ещё не решён). Имя в
state/, которого нет в RULES, попадает в отчёт как «без правила», и его
показывает doctor Свода (`check_state_cleanup`).

Отчёт `state/cleanup/last.json` в общем виде с очистителем ~/tmp Свода:
finished_at, deleted_files, freed_bytes, errors, error_sample, плюс overdue
(файлов старше срока осталось после прохода) и unknown (имена без правила).
"""
from __future__ import annotations

import fnmatch
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, paths

REPORT = Path("cleanup") / "last.json"
TWO_MIND_TTL_DAYS = 30
# Корни сервера telegram-mcp вне state/ (продолжение этапа 1, 09.11.2026):
# владелец бот, срок от последнего изменения (age_time), не зависит от
# ATTACHMENT_TTL_DAYS. Папка отправки: копии для send_file и вложения mailsend
# (paths.TELEGRAM_SEND_DIR); кэш: скачанное download_media. Системный tmpfiles для кэша
# пропал незаметно до 10.10.2026, поэтому уборка здесь, с отчётом для doctor.
TELEGRAM_FILES_TTL_DAYS = 30
# Из кэша только скачанное (download_media пишет в <корень>/downloads): в самом
# корне агенты держат свои файлы (аудио зала, документы), им срок не назначен.
TELEGRAM_MCP_DOWNLOADS = Path.home() / ".cache" / "telegram-mcp" / "downloads"


# Корни, куда агенты переносят и копируют файлы руками: там срок от позднего из
# mtime и ctime, иначе перенесённый (mv) или скопированный с датой (cp -p)
# старый файл ушёл бы первым же проходом. В state/ и у сервера Bot API файлы
# пишет сам бот: срок от mtime.
COPY_ROOTS = frozenset({paths.TELEGRAM_SEND_DIR, TELEGRAM_MCP_DOWNLOADS})


def age_time(st, root: Path) -> float:
    """Время, от которого считается срок файла дерева root."""
    return max(st.st_mtime, st.st_ctime) if root in COPY_ROOTS else st.st_mtime


def trees(state_dir: Path, attachment_ttl_days: int) -> list[tuple[Path, int, str]]:
    """Деревья суточной уборки: (корень, срок в днях, имя для журнала)."""
    return [
        (state_dir / "attachments", attachment_ttl_days, "attachments"),
        # Черновики записей памяти: тело уже в git памяти, файл нужен минуты.
        (state_dir / "memory-drafts", attachment_ttl_days, "memory-drafts"),
        # Запросы и ответы /2mind: нужны рану, который спросил; позже их не читают.
        (state_dir / "2mind", TWO_MIND_TTL_DAYS, "2mind"),
        (paths.TELEGRAM_SEND_DIR, TELEGRAM_FILES_TTL_DAYS, "telegram-send"),
        (TELEGRAM_MCP_DOWNLOADS, TELEGRAM_FILES_TTL_DAYS, "telegram-mcp-downloads"),
    ]


def due_files(roots: list[tuple[Path, int, str]], now: float) -> list[tuple[str, Path, float]]:
    """Что удалит проход (для сухого прогона): (имя дерева, файл, время срока).
    Тот же отбор, что у bot._cleanup_tree: по lstat, без входа в ссылки."""
    import os
    out = []
    for root, ttl, name in roots:
        if ttl <= 0 or root.is_symlink() or not root.is_dir():
            continue
        cutoff = now - ttl * 86400
        for top, dirs, names in os.walk(root, followlinks=False):
            for n in names + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
                f = Path(top, n)
                try:
                    m = age_time(f.lstat(), root)
                except OSError:
                    continue
                if m < cutoff:
                    out.append((name, f, m))
    return out

# Имя или шаблон верхнего уровня state/ -> правило. Порядок не важен.
RULES: dict[str, str] = {
    "attachments": "уборка бота: ATTACHMENT_TTL_DAYS от записи",
    "memory-drafts": "уборка бота: ATTACHMENT_TTL_DAYS от записи",
    "2mind": f"уборка бота: {TWO_MIND_TTL_DAYS} дней от записи (запросы и ответы /2mind, позже их никто не читает)",
    "audit.jsonl": "прежний общий журнал действий: разносится по месяцам первой уборкой",
    "audit-*.jsonl": "журнал действий по месяцам: audit.KEEP_MONTHS месяцев",
    "cleanup": "отчёт этой уборки",
    "*.lock": "замок: пустой файл, постоянно",
    "audit.jsonl.split.lock": "замок разноса журнала: пустой файл",
    "*.bak-*": "копия до ручной правки: удалять по «ок» Ивана",
    "telethon-bot.session*": "постоянно: вход Telethon (и журнал SQLite во время записи)",
    ".writetest-*": "проба записи при старте (__main__), живёт секунды",
    "geodata": "справочник: постоянно",
    # Этап 2: срок не решён, разбор в BACKLOG (уборка временного, этап 2).
    "events": "этап 2", "otkliki": "этап 2", "outbox": "этап 2", "news_digest": "этап 2",
    "voice-texts": "этап 2", "session_scopes.json": "этап 2", "codex_checks.json": "этап 2",
    "sent_texts.jsonl": "этап 2 (защита от повторов)",
    "deals": "этап 2", "diary": "этап 2", "diary_audio": "этап 2", "diary_evening": "этап 2",
    "handoff": "этап 2", "mailsend": "этап 2",
    # Словари состояния: один файл на вид, растут медленно; срок в этапе 2.
    "app_queue.json": "этап 2", "at_jobs.json": "этап 2", "claude_limits.json": "постоянно: одна запись",
    "cost_by_day.json": "этап 2", "daily_pins.json": "этап 2", "deal_sessions.json": "этап 2",
    "diary_snooze.json": "постоянно: одна запись", "foreign_sessions.json": "этап 2",
    "login_expiry.json": "постоянно: одна запись", "model_overrides.json": "постоянно: настройки",
    "model_pending_overrides.json": "постоянно: настройки", "model_session_overrides.json": "этап 2",
    "models.json": "постоянно: настройки", "reaction_notes.json": "этап 2",
    "scope_fingerprints.json": "этап 2", "scopes.json": "постоянно: настройки", "sessions.json": "этап 2",
    "session_costs.json": "этап 2", "topic_names.json": "этап 2", "untrusted_sessions.json": "этап 2",
    "inflight.json": "постоянно: идущие раны", "effort_overrides.json": "постоянно: настройки",
    "restart_request.json": "заявка на перезапуск: живёт до перезапуска",
}


def rule_for(name: str) -> str | None:
    if name in RULES:
        return RULES[name]
    return next((rule for pat, rule in RULES.items() if fnmatch.fnmatchcase(name, pat)), None)


def unknown(state_dir: Path) -> list[str]:
    """Имена верхнего уровня state/ без правила (скрытые временные `.имя.tmp`
    записи jsonstore не в счёт)."""
    try:
        names = sorted(p.name for p in state_dir.iterdir())
    except OSError:
        return []
    return [n for n in names if rule_for(n) is None and not (n.startswith(".") and n.endswith(".tmp"))]


def write_report(state_dir: Path, *, deleted: int, freed: int, errors: int, sample: list[str],
                 overdue: int) -> None:
    jsonstore.write_json(state_dir / REPORT, {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(state_dir), "deleted_files": deleted, "freed_bytes": freed,
        "errors": errors, "error_sample": sample[:3], "overdue": overdue,
        "unknown": unknown(state_dir),
        "python": ".".join(map(str, sys.version_info[:3])),
    }, indent=1)


def main(argv: list[str] | None = None) -> int:
    """python -m claude_tg_bot.retention --dry-run: что удалит следующий проход."""
    import argparse
    import time

    from dotenv import load_dotenv

    from . import config
    # Те же сроки и каталог, что у бота: его настройки из .env (значения не печатаются).
    load_dotenv(paths.ENV_FILE)
    ap = argparse.ArgumentParser(prog="claude_tg_bot.retention")
    ap.add_argument("--dry-run", action="store_true", required=True,
                    help="только показать; удаляет суточный цикл бота")
    ap.parse_args(argv)
    ttl = config._int_env("ATTACHMENT_TTL_DAYS", 30)
    roots = trees(paths.state_dir(), ttl)
    now = time.time()
    due = due_files(roots, now)
    for root, days, name in roots:
        print(f"{name}: {root}, срок {days} дн.")
    print(f"удалит {len(due)}:")
    for name, f, m in due:
        print(f"  {name}  {datetime.fromtimestamp(m, config.local_zone()):%d.%m.%Y}  {f}")
    unk = unknown(paths.state_dir())
    if unk:
        print("без правила: " + ", ".join(unk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
